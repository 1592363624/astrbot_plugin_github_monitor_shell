import base64
import json
import os
import uuid
from datetime import datetime
from typing import List, Dict, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star import StarTools

from ..utils.time_utils import format_commit_datetime

__all__ = ["NotificationService", "format_commit_datetime"]


class NotificationService:
    """通知服务类，负责向不同平台发送GitHub更新通知

    支持的QQ平台类型：
    - aiocqhttp: OneBot v11 协议（NapCat/Lagrange等）
    - qq_official: QQ官方API机器人
    - qq_official_webhook: QQ官方Webhook机器人

    推送目标支持两种格式：
    - 传统格式：纯数字群号/QQ号（自动匹配QQ系列平台）、"-"开头的Telegram群ID
    - UMO（unified_msg_origin，推荐）：平台ID:消息类型:会话ID，如
      aiocqhttp:GroupMessage:123456 或
      小爱同学:GroupMessage:0771687B325FC423AD9F4C06A88D84E3
      平台无关且自带路由信息，适配QQ官方机器人的非数字openid、多bot/多平台场景
    """

    # QQ系列平台类型列表，用于自动查找
    QQ_PLATFORM_TYPES = ["aiocqhttp", "qq_official", "qq_official_webhook"]

    def __init__(self, context, config: Dict | None = None, image_service=None, upload_service=None, onebot_sender=None):
        self.context = context
        self.image_service = image_service
        self.upload_service = upload_service
        self.onebot_sender = onebot_sender
        self.plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.failed_notifications_file = os.path.join(self.plugin_data_dir, "failed_notifications.json")
        self.time_zone = (config or {}).get("time_zone", "Asia/Shanghai")
        self.time_format = (config or {}).get("time_format", "%Y-%m-%d %H:%M:%S")
        # 从配置中获取平台ID（高级选项），如果未配置则自动查找QQ系列平台
        self.platform_id = (config or {}).get("platform_id", None)
        # 文转图相关配置（image_output 分组）
        image_cfg = (config or {}).get("image_output", {}) or {}
        self.commit_output_format = image_cfg.get("commit_output_format", "text")
        self.enable_base64_image = bool(image_cfg.get("enable_base64_image", True))
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        data_dir = os.path.dirname(self.failed_notifications_file)
        os.makedirs(data_dir, exist_ok=True)

    def _get_platform_id(self, platform_type: str = None, target: str = None) -> Optional[str]:
        """获取平台的ID

        查找优先级：
        1. 如果配置了 platform_id，直接使用（仅高级场景用）
        2. 若调用方显式指定了 platform_type，则按指定类型查找
        3. 自动查找 QQ 系列平台。多平台同时运行时按目标ID特征决定顺序：
           - 纯数字目标（传统QQ号/群号）→ aiocqhttp → qq_official → qq_official_webhook
           - 非数字目标（官方bot的十六进制openid）→ qq_official → qq_official_webhook → aiocqhttp
             官方bot没有数字QQ号/群号，非数字ID塞给 aiocqhttp 必然失败，故官方平台优先。

        Args:
            platform_type: 调用方显式指定的平台类型名称，如 "telegram"。
                          为 None 时按 QQ 系列平台自动检测。
            target: 推送目标字符串，用于按ID特征（数字/非数字）智能选择平台。

        Returns:
            平台的ID，如果未找到则返回None
        """
        # 如果配置了platform_id，直接返回（最高优先级）
        if self.platform_id:
            return self.platform_id

        # 确定要查找的平台类型列表
        if platform_type:
            # 调用方显式指定了类型（如 Telegram 专用分支）
            search_types = [platform_type]
        else:
            # 自动检测：根据目标ID特征决定QQ平台查找顺序
            # 官方bot（qq_official / qq_official_webhook）没有数字群号/QQ号，
            # 目标为非数字（如十六进制 openid）时应优先官方平台，而不是 aiocqhttp
            target_str = str(target or "").strip().lstrip("-")
            if target_str.isdigit():
                # 纯数字目标：OneBot（aiocqhttp）优先
                search_types = ["aiocqhttp", "qq_official", "qq_official_webhook"]
            else:
                # 非数字目标（openid等）：官方平台优先
                search_types = ["qq_official", "qq_official_webhook", "aiocqhttp"]

        # 在已注册的平台实例中查找
        for search_type in search_types:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if meta.name == search_type:
                    logger.info(f"自动检测到平台: {search_type} (ID: {meta.id})")
                    return meta.id

        # 未找到时输出调试信息
        available_platforms = [p.meta().name for p in self.context.platform_manager.platform_insts]
        logger.warning(
            f"未找到匹配的平台。查找类型: {search_types}，当前可用平台: {available_platforms}"
        )
        return None

    @staticmethod
    def _parse_umo(target: str) -> Optional[MessageSesion]:
        """尝试把目标字符串解析为 UMO（unified_msg_origin）。

        格式: 平台ID:消息类型:会话ID，例如:
        - aiocqhttp:GroupMessage:123456
        - 小祥²:GroupMessage:0771687B325FC423AD9F4C06A88D84E3
        - qq_official:FriendMessage:ABCDEF0123456789

        消息类型同时兼容枚举值（GroupMessage）与枚举名（GROUP_MESSAGE），
        避免用户在 WebUI 复制到枚举名时解析失败而静默回退到自动检测。

        解析失败（格式不符 / 消息类型非法）返回 None，由调用方回退到传统目标格式。
        """
        if not isinstance(target, str) or target.count(":") < 2:
            return None
        platform_id, message_type_str, session_id = target.split(":", 2)
        # 消息类型兼容：先按枚举值（GroupMessage）解析，失败再按枚举名（GROUP_MESSAGE）解析
        try:
            message_type = MessageType(message_type_str)
        except ValueError:
            try:
                message_type = MessageType[message_type_str]
            except (KeyError, ValueError, TypeError):
                return None
        if not isinstance(message_type, MessageType):
            return None
        return MessageSesion(
            platform_name=platform_id,
            message_type=message_type,
            session_id=session_id,
        )

    def _is_platform_of_type(self, platform_id: str, platform_type: str) -> bool:
        """检查指定平台ID的实例是否为给定平台类型（如 aiocqhttp）"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if meta.id == platform_id:
                    return meta.name == platform_type
        except Exception as e:
            logger.debug(f"检查平台类型时出错: {str(e)}")
        return False

    def _resolve_onebot_digit_id(
        self, target: str, session: Optional[MessageSesion] = None
    ) -> Optional[str]:
        """若目标可经 OneBot 直发（数字ID且能确定是 aiocqhttp 平台），返回数字ID，否则返回 None"""
        if session is not None:
            if session.session_id.isdigit() and self._is_platform_of_type(
                session.platform_name, "aiocqhttp"
            ):
                return session.session_id
            return None
        target_str = str(target).strip()
        return target_str if target_str.isdigit() else None

    async def _send_by_session(self, session: MessageSesion, message, target_desc: str = ""):
        """通过已构造好的会话对象发送消息（StarTools 通道，平台无关）"""
        try:
            message_chain = self._to_message_chain(message)
            sent = await StarTools.send_message(session, message_chain)
            if not sent:
                error_msg = f"发送消息失败: 找不到平台 {session.platform_name}，请检查平台是否已启动"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}
            logger.info(f"✅ 成功向 {target_desc or session.session_id} 发送消息")
            return {"success": True}
        except Exception as e:
            error_msg = f"发送消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    def _load_failed_notifications(self) -> List:
        """加载发送失败的通知"""
        try:
            if os.path.exists(self.failed_notifications_file):
                with open(self.failed_notifications_file, 'r', encoding='utf-8') as f:
                    data = json.load(f) or []
                    if not isinstance(data, list):
                        return []
                    data = self._normalize_failed_notifications(data)
                    data = self._dedupe_failed_notifications(data)
                    # 清理过期的通知数据（比如仓库已删除的通知）
                    valid_notifications = [n for n in data if self._is_notification_valid(n)]
                    if len(valid_notifications) != len(data):
                        self._save_failed_notifications(valid_notifications)
                    return valid_notifications
            return []
        except Exception as e:
            logger.error(f"加载失败通知记录失败: {str(e)}")
            return []

    def _normalize_failed_notifications(self, notifications: List[Dict]) -> List[Dict]:
        normalized: List[Dict] = []
        for n in notifications:
            if not isinstance(n, dict):
                continue
            repo_info = n.get("repo_info")
            new_commits = n.get("new_commits")
            if not isinstance(repo_info, dict) or not isinstance(new_commits, list) or not new_commits:
                continue

            targets = n.get("targets", [])
            group_targets = n.get("group_targets", [])
            if targets is None:
                targets = []
            if group_targets is None:
                group_targets = []

            item = {
                "repo_info": repo_info,
                "new_commits": new_commits,
                "targets": self._normalize_target_list(targets),
                "group_targets": self._normalize_target_list(group_targets),
                "branch": n.get("branch"),
            }
            item["key"] = n.get("key") or self._build_notification_key(repo_info, new_commits)
            item["attempts"] = int(n.get("attempts", 0) or 0)
            item["created_at"] = n.get("created_at") or datetime.utcnow().isoformat()
            normalized.append(item)
        return normalized

    def _dedupe_failed_notifications(self, notifications: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {}
        for n in notifications:
            key = n.get("key")
            if not key:
                continue
            if key not in merged:
                merged[key] = n
                continue

            existing = merged[key]
            existing["targets"] = self._merge_unique(existing.get("targets", []), n.get("targets", []))
            existing["group_targets"] = self._merge_unique(
                existing.get("group_targets", []),
                n.get("group_targets", []),
            )
            existing["attempts"] = max(int(existing.get("attempts", 0) or 0), int(n.get("attempts", 0) or 0))
            existing_created_at = existing.get("created_at")
            n_created_at = n.get("created_at")
            if isinstance(existing_created_at, str) and isinstance(n_created_at, str):
                existing["created_at"] = min(existing_created_at, n_created_at)
        return list(merged.values())

    def _merge_unique(self, a: List[str], b: List[str]) -> List[str]:
        merged = []
        seen = set()
        for item in (a or []) + (b or []):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
        return merged

    def _normalize_target_list(self, items) -> List[str]:
        if not isinstance(items, list):
            return []
        cleaned: List[str] = []
        for x in items:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            cleaned.append(s)
        return cleaned

    def _build_notification_key(self, repo_info: Dict, new_commits: List[Dict]) -> str:
        owner = (repo_info.get("owner") or {}).get("login") or "unknown"
        repo = repo_info.get("name") or "unknown"
        sha = ""
        if new_commits and isinstance(new_commits[0], dict):
            sha = new_commits[0].get("sha") or ""
        return f"{owner}/{repo}@{sha}"

    def _is_notification_valid(self, notification: Dict) -> bool:
        """检查通知是否仍然有效（仓库是否仍然在配置中）"""
        try:
            # 获取插件实例来访问配置
            github_plugin = None
            for star in self.context.get_all_stars():
                if star.name == "GitHub监控插件":
                    github_plugin = star.star_cls
                    break

            if github_plugin and github_plugin.config:
                repositories = github_plugin.config.get("repositories", "")
                repo_info = notification.get("repo_info", {})

                # 检查仓库是否仍在配置中
                for repo_config in repositories:
                    if isinstance(repo_config, str):
                        # 字符串格式: "owner/repo|group1|group2|..."
                        parts = repo_config.split("|")
                        repo_path = parts[0]
                        if "/" in repo_path:
                            owner, repo = repo_path.split("/", 1)
                            if (owner == repo_info.get('owner', {}).get('login') and
                                    repo == repo_info.get('name')):
                                return True
                    elif isinstance(repo_config, dict):
                        # 字典格式: {"owner": "...", "repo": "...", "groups": [...], ...}
                        if (repo_config.get("owner") == repo_info.get('owner', {}).get('login') and
                                repo_config.get("repo") == repo_info.get('name')):
                            return True
            # 如果无法确定，保留通知（宁可多发也不漏发）
            return True
        except Exception as e:
            logger.error(f"检查通知有效性时出错: {str(e)}")
            # 出错时保留通知
            return True

    def _save_failed_notifications(self, notifications: List):
        """保存发送失败的通知"""
        try:
            with open(self.failed_notifications_file, 'w', encoding='utf-8') as f:
                normalized = self._normalize_failed_notifications(notifications)
                normalized = self._dedupe_failed_notifications(normalized)
                json.dump(normalized, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存失败通知记录失败: {str(e)}")

    async def retry_failed_notifications(self):
        """重试发送失败的通知"""
        failed_notifications = self._load_failed_notifications()
        if not failed_notifications:
            return

        logger.info(f"尝试重新发送 {len(failed_notifications)} 条失败的通知")
        remaining_notifications = []

        for notification in failed_notifications:
            notification_key = notification.get("key")
            targets = notification.get("targets", [])
            group_targets = notification.get("group_targets", [])

            # 获取最新commit信息，检查是否已经发送过
            repo_info = notification.get("repo_info", {})
            new_commits = notification.get("new_commits", [])

            # 检查是否已经在主发送记录中标记为已发送
            if self._is_already_sent_in_main_record(repo_info, new_commits, targets, group_targets):
                logger.info(f"通知 {notification_key} 已经在主记录中标记为已发送，跳过重试")
                continue

            failed_targets, failed_group_targets = await self._send_notification_collect_failures(
                repo_info,
                new_commits,
                targets,
                group_targets,
                branch=notification.get("branch"),
            )

            if failed_targets or failed_group_targets:
                notification["targets"] = failed_targets
                notification["group_targets"] = failed_group_targets
                notification["attempts"] = int(notification.get("attempts", 0) or 0) + 1
                remaining_notifications.append(notification)
            else:
                # 发送成功，标记为主已发送
                self._mark_as_sent_in_main_record(repo_info, new_commits, targets, group_targets)

        # 保存仍然失败的通知
        self._save_failed_notifications(remaining_notifications)
        logger.info(f"重试后仍失败的通知数量: {len(remaining_notifications)}")

    def _is_already_sent_in_main_record(self, repo_info: Dict, new_commits: List[Dict], targets: List[str], group_targets: List[str]) -> bool:
        """检查通知是否已经在主发送记录中"""
        try:
            from astrbot.core.star import StarTools
            plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
            sent_file = os.path.join(plugin_data_dir, "sent_notifications.json")

            if not os.path.exists(sent_file):
                return False

            with open(sent_file, 'r', encoding='utf-8') as f:
                sent_data = json.load(f)

            if not sent_data or not new_commits:
                return False

            owner = (repo_info.get("owner") or {}).get("login") or ""
            repo = repo_info.get("name") or ""
            repo_key = f"{owner}/{repo}"

            latest_sha = new_commits[0].get("sha", "") if new_commits else ""
            if not latest_sha:
                return False

            repo_sent_data = sent_data.get(repo_key, {})
            commit_sent_data = repo_sent_data.get(latest_sha, [])

            # 检查是否有任何记录包含当前的目标列表
            target_set = set(str(t) for t in targets)
            group_set = set(str(g) for g in group_targets)

            for sent_groups in commit_sent_data:
                sent_group_set = set(str(g) for g in sent_groups)
                # 如果当前群组列表是已发送列表的子集，认为已发送
                if group_set.issubset(sent_group_set):
                    return True

            return False
        except Exception:
            return False

    def _mark_as_sent_in_main_record(self, repo_info: Dict, new_commits: List[Dict], targets: List[str], group_targets: List[str]):
        """在主发送记录中标记为已发送"""
        try:
            from astrbot.core.star import StarTools
            plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
            sent_file = os.path.join(plugin_data_dir, "sent_notifications.json")

            sent_data = {}
            if os.path.exists(sent_file):
                with open(sent_file, 'r', encoding='utf-8') as f:
                    sent_data = json.load(f)

            owner = (repo_info.get("owner") or {}).get("login") or ""
            repo = repo_info.get("name") or ""
            repo_key = f"{owner}/{repo}"

            latest_sha = new_commits[0].get("sha", "") if new_commits else ""
            if not latest_sha:
                return

            if repo_key not in sent_data:
                sent_data[repo_key] = {}
            if latest_sha not in sent_data[repo_key]:
                sent_data[repo_key][latest_sha] = []

            group_list = list(set(str(g) for g in group_targets))
            if group_list and group_list not in sent_data[repo_key][latest_sha]:
                sent_data[repo_key][latest_sha].append(group_list)

            with open(sent_file, 'w', encoding='utf-8') as f:
                json.dump(sent_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"标记通知为已发送失败: {str(e)}")

    async def send_commit_notification(self, repo_info: Dict, new_commits: List[Dict], targets: List[str],
                                       group_targets: List[str] = None, branch: str = None):
        """发送commit变更通知"""
        # 检查是否有有效的提交
        if not new_commits:
            logger.info("没有新的提交需要通知")
            return

        try:
            failed_targets, failed_group_targets = await self._send_notification_collect_failures(
                repo_info,
                new_commits,
                targets,
                group_targets,
                branch=branch,
            )

            if failed_targets or failed_group_targets:
                failed_notifications = self._load_failed_notifications()
                failed_notifications.append(
                    {
                        "repo_info": repo_info,
                        "new_commits": new_commits,
                        "targets": failed_targets,
                        "group_targets": failed_group_targets,
                        "branch": branch,
                        "key": self._build_notification_key(repo_info, new_commits),
                        "attempts": 1,
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )
                self._save_failed_notifications(failed_notifications)
                logger.warning("部分通知发送失败，已保存到待重试列表")
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")
            # 保存到失败列表中
            try:
                failed_notifications = self._load_failed_notifications()
                failed_notifications.append(
                    {
                        "repo_info": repo_info,
                        "new_commits": new_commits,
                        "targets": self._normalize_target_list(targets),
                        "group_targets": self._normalize_target_list(group_targets),
                        "branch": branch,
                        "key": self._build_notification_key(repo_info, new_commits),
                        "attempts": 1,
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )
                self._save_failed_notifications(failed_notifications)
                logger.warning("通知发送异常，已保存到待重试列表")
            except Exception as save_error:
                logger.error(f"保存失败通知记录也失败了: {str(save_error)}")

    async def _send_notification_collect_failures(
        self,
        repo_info: Dict,
        new_commits: List[Dict],
        targets,
        group_targets=None,
        branch: str = None,
    ) -> tuple[List[str], List[str]]:
        try:
            text_message = self._format_commit_message(repo_info, new_commits)
            message: str | MessageChain = text_message
            image_b64: Optional[str] = None
            temp_image_path: Optional[str] = None

            # 文转图模式：渲染 commit 卡片（渲染一次，所有目标复用）
            if self.commit_output_format == "image":
                if not self.image_service:
                    logger.error("文转图服务未注入，无法渲染 commit 卡片")
                else:
                    try:
                        image_b64 = await self.image_service.render_commit_image(
                            repo_info, new_commits, branch
                        )
                    except Exception as e:
                        logger.error(f"渲染 commit 卡片图片失败: {str(e)}")
                if image_b64:
                    temp_image_path = self._write_temp_image(image_b64)
                    message = self._build_image_chain(image_b64, temp_image_path)
                else:
                    # 渲染失败：不发送任何内容，所有目标计入失败，交由重试队列下轮重试
                    logger.error("commit 卡片图片渲染失败，本次通知未发送，将进入重试队列")
                    return (
                        self._normalize_target_list(targets),
                        self._normalize_target_list(group_targets),
                    )

            failed_targets: List[str] = []
            failed_group_targets: List[str] = []

            try:
                for target in self._merge_unique(self._normalize_target_list(targets), []):
                    try:
                        result = await self._send_to_target(
                            target, False, message, image_b64, temp_image_path
                        )
                        if not result.get("success", False):
                            failed_targets.append(target)
                    except Exception:
                        failed_targets.append(target)

                for group_target in self._merge_unique(self._normalize_target_list(group_targets), []):
                    try:
                        result = await self._send_to_target(
                            group_target, True, message, image_b64, temp_image_path
                        )
                        image_sent = bool(image_b64 and result.get("success", False))
                        upload_id = (
                            self._resolve_onebot_digit_id(group_target)
                            if image_sent and self.upload_service
                            else None
                        )
                        if not result.get("success", False):
                            failed_group_targets.append(group_target)
                        elif upload_id is not None:
                            # 图片通知发送成功后，按配置备份上传到群文件/群相册
                            try:
                                await self.upload_service.maybe_upload(
                                    upload_id,
                                    image_b64,
                                    self._build_image_filename(repo_info, new_commits),
                                )
                            except Exception as e:
                                logger.error(f"群文件/相册上传出错: {str(e)}")
                    except Exception:
                        failed_group_targets.append(group_target)
            finally:
                if temp_image_path:
                    try:
                        os.remove(temp_image_path)
                    except OSError:
                        pass

            total_p = len(self._normalize_target_list(targets))
            total_g = len(self._normalize_target_list(group_targets))
            logger.info(
                f"通知发送完成：私聊 {total_p - len(failed_targets)}/{total_p} 成功，"
                f"群 {total_g - len(failed_group_targets)}/{total_g} 成功"
            )
            return failed_targets, failed_group_targets
        except Exception as e:
            logger.error(f"发送通知时发生异常: {str(e)}")
            return self._normalize_target_list(targets), self._normalize_target_list(group_targets)

    async def _send_to_target(
        self,
        target: str,
        is_group: bool,
        message,
        image_b64: Optional[str],
        temp_image_path: Optional[str],
    ):
        """发送通知到单个目标。

        支持的目标格式：
        - UMO（平台ID:消息类型:会话ID，推荐，平台无关，如QQ官方机器人的openid场景）
        - 纯数字群号/QQ号：自动匹配QQ系列平台
        - 以 "-" 开头的ID：Telegram 群组

        图片模式下，aiocqhttp 平台的数字目标使用 OneBot 直发；其余情况
        （其他平台、Telegram 群等）使用 StarTools 消息链通道。
        """
        target_str = str(target).strip()
        session = self._parse_umo(target_str)

        # 图片优先尝试 OneBot 直发通道（仅限数字ID且能确定是 aiocqhttp 平台）
        if image_b64 and temp_image_path and self.onebot_sender:
            direct_id = self._resolve_onebot_digit_id(target_str, session)
            if direct_id is not None:
                try:
                    direct_ok = await self.onebot_sender.send_image(
                        direct_id, image_b64, temp_image_path, is_group=is_group
                    )
                except Exception as e:
                    logger.error(f"OneBot 直发图片出错: {str(e)}")
                    direct_ok = False
                if direct_ok is not None:
                    return {"success": direct_ok}

        # UMO 目标自带平台与会话类型信息，直接路由发送
        if session is not None:
            return await self._send_by_session(session, message, target_desc=target_str)

        if is_group:
            return await self._send_group_message(target_str, message)
        return await self._send_private_message(target_str, message)

    def _write_temp_image(self, image_b64: str) -> str:
        """把 base64 图片写入数据目录的临时文件，返回文件路径（由调用方发送后清理）"""
        temp_dir = os.path.join(self.plugin_data_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"commit_card_{uuid.uuid4().hex}.png")
        with open(temp_path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        return temp_path

    def _build_image_chain(self, image_b64: str, temp_path: str) -> MessageChain:
        """根据配置构建图片消息链（StarTools 通道使用）。

        优先使用 Base64；关闭 Base64 或当前 AstrBot 版本不支持 fromBase64 时，
        使用临时文件 + fromFileSystem。注意：经 StarTools 发送时 AstrBot 会将
        Image 段统一转为 base64://，该配置只在 OneBot 直发通道下影响传输模式。
        """
        from_base64 = getattr(Comp.Image, "fromBase64", None)
        if self.enable_base64_image and callable(from_base64):
            return MessageChain(chain=[from_base64(image_b64)])
        return MessageChain(chain=[Comp.Image.fromFileSystem(temp_path)])

    @staticmethod
    def _build_image_filename(repo_info: Dict, new_commits: List[Dict]) -> str:
        owner = (repo_info.get("owner") or {}).get("login") or "unknown"
        repo = repo_info.get("name") or "unknown"
        sha = (new_commits[0].get("sha") or "")[:7] if new_commits else ""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{sha}" if sha else ""
        return f"github_commit_{owner}_{repo}{suffix}_{timestamp}.png"

    def _format_commit_message(self, repo_info: Dict, new_commits: List[Dict]) -> str:
        """格式化commit消息"""
        repo_name = f"{repo_info['owner']['login']}/{repo_info['name']}"

        message = f"🔔 GitHub仓库更新通知\n\n"
        message += f"📁 仓库: {repo_name}\n"
        message += f"🔗 链接: {repo_info['html_url']}\n\n"

        if len(new_commits) == 1:
            # 只有一个提交的向后兼容格式
            commit = new_commits[0]
            formatted_date = format_commit_datetime(
                commit["date"],
                self.time_zone,
                self.time_format,
            )
            message += f"✨ 新Commit:\n"
            message += f"📝 SHA: {commit['sha'][:7]}\n"
            message += f"👤 作者: {commit['author']}\n"
            if formatted_date:
                message += f"📅 时间: {formatted_date}\n"
            else:
                message += f"📅 时间: {commit['date']}\n"
            message += f"💬 信息: {commit['message']}\n"
            message += f"🔗 链接: {commit['url']}\n\n"
        else:
            # 有多个提交的格式
            message += f"✨ 本次更新包含 {len(new_commits)} 个新提交:\n\n"
            for i, commit in enumerate(new_commits, 1):
                formatted_date = format_commit_datetime(
                    commit["date"],
                    self.time_zone,
                    self.time_format,
                )
                message += f"{i}. ✨ 新Commit:\n"
                message += f"   📝 SHA: {commit['sha'][:7]}\n"
                message += f"   👤 作者: {commit['author']}\n"
                if formatted_date:
                    message += f"   📅 时间: {formatted_date}\n"
                else:
                    message += f"   📅 时间: {commit['date']}\n"
                message += f"   💬 信息: {commit['message']}\n"
                message += f"   🔗 链接: {commit['url']}\n\n"

        return message

    @staticmethod
    def _to_message_chain(message) -> MessageChain:
        """str 转为纯文本消息链，MessageChain 原样返回"""
        if isinstance(message, MessageChain):
            return message
        return MessageChain().message(message)

    async def _send_private_message(self, user_id, message):
        """通过 AstrBot 通用接口主动发送私聊消息

        使用 MessageSession 构造会话对象，通过 StarTools.send_message 发送消息。
        支持 aiocqhttp / qq_official / qq_official_webhook 等QQ系列平台。
        支持的目标格式：
        - UMO: 平台ID:FriendMessage:会话ID（推荐，平台无关，多bot场景可用）
        - 纯数字QQ号 / 非数字会话ID（如QQ官方机器人的十六进制openid，自动匹配QQ系列平台）
        message 支持纯文本 str 或 MessageChain（如图片消息）。
        """
        try:
            user_id_str = str(user_id).strip()

            # UMO 格式：自带平台与会话类型信息，直接路由发送
            session = self._parse_umo(user_id_str)
            if session is not None:
                return await self._send_by_session(session, message, target_desc=user_id_str)

            # 获取平台ID（自动检测 QQ 系列平台，按目标ID特征智能选择，或使用配置指定的类型）
            platform_id = self._get_platform_id(target=user_id_str)
            if not platform_id:
                error_msg = (
                    "发送私聊消息失败: 未找到QQ平台实例。"
                    "请确保已启动 aiocqhttp / qq_official / qq_official_webhook 平台之一，"
                    "或在配置中指定 platform_id / platform_type"
                )
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            # 构造私聊会话对象
            session = MessageSesion(
                platform_name=platform_id,
                message_type=MessageType.FRIEND_MESSAGE,
                session_id=user_id_str,
            )
            message_chain = self._to_message_chain(message)
            sent = await StarTools.send_message(session, message_chain)

            if not sent:
                error_msg = f"发送私聊消息失败: 找不到平台 {platform_id}，请检查平台是否已启动"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            logger.info(f"✅ 成功向 {user_id_str} 发送私聊消息")
            return {"success": True}
        except Exception as e:
            error_msg = f"发送私聊消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    async def _send_group_message(self, group_id, message):
        """通过 AstrBot 通用接口主动发送群消息

        支持的群目标格式：
        - UMO: 平台ID:GroupMessage:会话ID（推荐，平台无关，多bot场景可用）
        - 纯数字群号: 自动匹配QQ系列平台（aiocqhttp / qq_official / qq_official_webhook）
        - 以 "-" 开头的ID: Telegram 群组
        - 其他非数字会话ID（如QQ官方机器人的十六进制openid）: 自动匹配QQ系列平台
        message 支持纯文本 str 或 MessageChain（如图片消息）。
        """
        try:
            group_id_str = str(group_id).strip()

            # UMO 格式：自带平台与会话类型信息，直接路由发送
            session = self._parse_umo(group_id_str)
            if session is not None:
                return await self._send_by_session(session, message, target_desc=group_id_str)

            if group_id_str.startswith("-"):
                # Telegram 群组（以负号开头的 chat_id）
                platform_id = None
                for platform in self.context.platform_manager.platform_insts:
                    meta = platform.meta()
                    if meta.name == "telegram":
                        platform_id = meta.id
                        break
                if not platform_id:
                    error_msg = "发送群消息失败: 未找到Telegram适配器"
                    logger.error(error_msg)
                    return {"success": False, "message": error_msg}

                session = MessageSesion(
                    platform_name=platform_id,
                    message_type=MessageType.GROUP_MESSAGE,
                    session_id=group_id_str,
                )
                sent = await StarTools.send_message(session, self._to_message_chain(message))
                if not sent:
                    error_msg = f"发送群消息失败: 找不到平台 {platform_id}"
                    logger.error(error_msg)
                    return {"success": False, "message": error_msg}
                logger.info(f"✅ 成功向 Telegram 群 {group_id_str} 发送消息")
                return {"success": True}

            # QQ系列群：纯数字群号，或其他非数字会话ID（如QQ官方机器人的openid）
            # 自动检测 QQ 系列平台，按目标ID特征智能选择（非数字openid → 官方平台优先）
            platform_id = self._get_platform_id(target=group_id_str)
            if not platform_id:
                error_msg = (
                    "发送群消息失败: 未找到QQ平台实例。"
                    "请确保已启动 aiocqhttp / qq_official / qq_official_webhook 平台之一，"
                    "或在配置中指定 platform_id / platform_type"
                )
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            # 构造 QQ 群会话对象
            session = MessageSesion(
                platform_name=platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=group_id_str,
            )
            sent = await StarTools.send_message(session, self._to_message_chain(message))
            if not sent:
                error_msg = f"发送群消息失败: 找不到平台 {platform_id}，请检查平台是否已启动"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}
            logger.info(f"✅ 成功向 QQ 群 {group_id_str} 发送消息")
            return {"success": True}
        except Exception as e:
            error_msg = f"发送群消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
