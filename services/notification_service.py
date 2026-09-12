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

from ..utils.file_utils import write_json_atomic
from ..utils.text_utils import split_long_message
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
        # 合并转发待发送队列的落盘文件：队列在内存中的同时持久化，
        # 保证进程崩溃 / 刷新异常后仍能恢复未发出的通知
        self.pending_commit_notifications_file = os.path.join(
            self.plugin_data_dir, "pending_forward_notifications.json"
        )
        self.time_zone = (config or {}).get("time_zone", "Asia/Shanghai")
        self.time_format = (config or {}).get("time_format", "%Y-%m-%d %H:%M:%S")
        # 从配置中获取平台ID（高级选项），如果未配置则自动查找QQ系列平台
        self.platform_id = (config or {}).get("platform_id", None)
        # 文转图相关配置（image_output 分组）
        image_cfg = (config or {}).get("image_output", {}) or {}
        self.commit_output_format = image_cfg.get("commit_output_format", "text")
        self.enable_base64_image = bool(image_cfg.get("enable_base64_image", True))
        # 单条纯文本消息安全长度：超过后自动拆分为多条发送（QQ 对单条消息长度有限制）
        self.message_max_length = int((config or {}).get("message_max_length", 2000) or 2000)
        # 合并转发推送相关配置（forward_merge 分组）
        forward_cfg = (config or {}).get("forward_merge", {}) or {}
        self.forward_merge_enabled = bool(forward_cfg.get("enabled", False))
        self.forward_content_mode = str(forward_cfg.get("content_mode") or "follow").strip().lower()
        self.forward_node_name = str(forward_cfg.get("node_name") or "GitHub监控")
        self.forward_node_uin = str(forward_cfg.get("node_uin") or "10000")
        self.forward_max_nodes = max(1, int(forward_cfg.get("max_nodes_per_message", 20) or 20))
        self.forward_fallback_to_normal = bool(forward_cfg.get("fallback_to_normal", True))
        # 待合并的 commit 通知队列：轮询期间入队，本轮检查结束后由 flush_commit_notifications 统一发送
        # 队列同步落盘，启动时恢复上次未发送完的条目（见 _load_pending_commit_notifications）
        self._pending_commit_notifications: List[Dict] = []
        self._ensure_data_dir()
        # 重启后自动补发上次因崩溃 / 异常而未发送完的通知
        self._pending_commit_notifications = self._load_pending_commit_notifications()

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
                # 合并转发模式的重试元数据：决定该条目重试时走合并转发发送器
                "forward_merge": bool(n.get("forward_merge", False)),
                "forward_batch": bool(n.get("forward_batch", False)),
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
            normalized = self._normalize_failed_notifications(notifications)
            normalized = self._dedupe_failed_notifications(normalized)
            write_json_atomic(self.failed_notifications_file, normalized)
        except Exception as e:
            logger.error(f"保存失败通知记录失败: {str(e)}")

    async def retry_failed_notifications(self):
        """重试发送失败的通知"""
        failed_notifications = self._load_failed_notifications()
        if not failed_notifications:
            return

        logger.info(f"尝试重新发送 {len(failed_notifications)} 条失败的通知")
        remaining_notifications = []
        # 合并转发模式下失败的目标：按目标重新合并成批次后统一重试
        forward_retry_entries: List[Dict] = []

        for notification in failed_notifications:
            notification_key = notification.get("key")
            targets = notification.get("targets", [])
            group_targets = notification.get("group_targets", [])

            # 获取最新commit信息，检查是否已经发送过
            repo_info = notification.get("repo_info", {})
            new_commits = notification.get("new_commits", [])

            # 合并转发模式下失败的目标不能走普通逐条发送（会把一条合并前向消息拆成
            # 多条普通消息），先收集起来，最后按目标重新合并成批次重试。
            # 注意这里不查主发送记录：整合发送失败时调用方仍可能已把该提交标记为
            # 已通知，条目还在待重试队列里就说明它没真正发出去，必须照常重试。
            if notification.get("forward_merge") and notification.get("forward_batch"):
                forward_retry_entries.append(notification)
                continue

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
        # 合并转发重试：同一目标的多条通知重新合并成一条转发记录发送
        if forward_retry_entries:
            remaining_notifications.extend(
                await self._retry_forward_notifications(forward_retry_entries)
            )

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

            write_json_atomic(sent_file, sent_data)
        except Exception as e:
            logger.error(f"标记通知为已发送失败: {str(e)}")

    def _load_pending_commit_notifications(self) -> List[Dict]:
        """从磁盘恢复合并转发待发送队列

        队列在内存中的同时会落盘（见 _save_pending_commit_notifications）：进程
        崩溃、或整合发送过程中抛出未处理异常时，提交数据可能已经推进到最新 SHA，
        但通知还没发出去。启动时恢复这些条目并在本轮重新入队补发，避免通知永久丢失。
        运行时状态（failed_targets 等）不入盘，恢复时重置为空。
        """
        try:
            if not os.path.exists(self.pending_commit_notifications_file):
                return []
            with open(self.pending_commit_notifications_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"加载合并转发待发送队列失败: {str(e)}")
            return []

        if not isinstance(data, list):
            return []

        restored: List[Dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            repo_info = item.get("repo_info")
            new_commits = item.get("new_commits")
            if not isinstance(repo_info, dict) or not isinstance(new_commits, list) or not new_commits:
                continue
            targets = self._normalize_target_list(item.get("targets", []))
            group_targets = self._normalize_target_list(item.get("group_targets", []))
            if not targets and not group_targets:
                continue
            restored.append({
                "repo_key": item.get("repo_key"),
                "repo_info": repo_info,
                "new_commits": new_commits,
                "targets": targets,
                "group_targets": group_targets,
                "branch": item.get("branch"),
                "failed_targets": [],
                "failed_group_targets": [],
            })

        if restored:
            logger.info(f"合并转发：已恢复 {len(restored)} 条上次未发送完的通知，将在本轮统一补发")
        return restored

    def _save_pending_commit_notifications(self):
        """把合并转发待发送队列原子落盘（失败只记日志，不影响本轮推送）"""
        try:
            write_json_atomic(
                self.pending_commit_notifications_file,
                self._pending_commit_notifications,
            )
        except Exception as e:
            logger.error(f"保存合并转发待发送队列失败: {str(e)}")

    # ------------------------------------------------------------------
    # 合并转发（以一条转发聊天记录承载本轮所有 commit 通知）
    # ------------------------------------------------------------------

    def queue_commit_notification(self, repo_info: Dict, new_commits: List[Dict],
                                  targets=None, group_targets=None, branch: str = None,
                                  repo_key: str = None):
        """把本轮检测到的 commit 通知放入待合并队列（不立即发送）。

        合并转发模式下，一次轮询中所有仓库的 commit 通知都会先入队，待本轮
        检查结束后由 :meth:`flush_commit_notifications` 按推送目标整合成一条
        （或按节点上限拆分的多条）合并转发聊天记录统一发送。

        Args:
            repo_info: 仓库信息（GitHub API 返回值）。
            new_commits: 本次检测到的新提交列表。
            targets: 私聊推送目标列表。
            group_targets: 群聊推送目标列表。
            branch: 实际监控的分支名，仅用于图片卡片展示。
            repo_key: 调用方的仓库键（owner/repo/branch），用于回传标记已通知。
        """
        if not new_commits:
            return

        # 防重复入队：同一仓库同一提交在同一批合并转发中只出现一次
        # （例如上一轮整合发送中途异常，队列未清空而本轮又重新检测到该提交）
        latest_sha = (new_commits[0] or {}).get("sha") or ""
        if repo_key and latest_sha:
            for queued in self._pending_commit_notifications:
                queued_commits = queued.get("new_commits") or []
                queued_sha = (queued_commits[0] or {}).get("sha") if queued_commits else ""
                if queued.get("repo_key") == repo_key and queued_sha == latest_sha:
                    logger.info(
                        f"合并转发：{repo_key} 的提交 {latest_sha[:7]} 已在待发送队列中，跳过重复入队"
                    )
                    return

        self._pending_commit_notifications.append({
            "repo_key": repo_key,
            "repo_info": repo_info,
            "new_commits": new_commits,
            "targets": self._merge_unique(self._normalize_target_list(targets), []),
            "group_targets": self._merge_unique(self._normalize_target_list(group_targets), []),
            "branch": branch,
            "failed_targets": [],
            "failed_group_targets": [],
        })
        # 入队即落盘：即使随后进程退出，队列也能在下次启动时恢复补发
        self._save_pending_commit_notifications()

    async def flush_commit_notifications(self) -> List[Dict]:
        """把队列中收集到的 commit 通知整合成合并转发消息发送。

        按推送目标分组：每个目标收到一条（或按 ``max_nodes_per_message`` 拆分的
        多条）转发聊天记录，节点内容为本轮投递给该目标的所有 commit 通知。
        qq_official / Telegram 等不支持合并转发的平台会按 ``fallback_to_normal``
        决定降级为逐条普通消息，或直接判定发送失败。

        Returns:
            本轮出队的通知条目列表（元素含 ``repo_key`` / ``new_commits`` /
            ``group_targets``），调用方应据此把提交标记为已通知——即使部分目标
            发送失败，失败目标也会写入待重试队列，避免下轮重复入队造成重复推送。
        """
        # 先清空内存队列，让落盘文件与「发送中/发送失败」的条目一一对应：
        # 发送失败的目标会连同本条目重写回落盘文件，异常退出也不会丢失通知。
        pending = self._pending_commit_notifications
        self._pending_commit_notifications = []
        self._save_pending_commit_notifications()
        if not pending:
            return []

        logger.info(f"合并转发：本轮共收集到 {len(pending)} 条 commit 通知，开始整合发送")

        # 按 (是否群聊, 推送目标) 分组，保持检测到的先后顺序即为节点顺序
        grouped: Dict[tuple, List[Dict]] = {}
        for item in pending:
            for target in item["targets"]:
                grouped.setdefault((False, target), []).append(item)
            for target in item["group_targets"]:
                grouped.setdefault((True, target), []).append(item)

        for (is_group, target), items in grouped.items():
            try:
                await self._send_merged_forward(is_group, target, items)
            except Exception as e:
                logger.error(f"合并转发发送到 {target} 出错: {str(e)}", exc_info=True)
                self._mark_target_failed(is_group, target, items)

        failed_payloads = []
        for item in pending:
            if not item["failed_targets"] and not item["failed_group_targets"]:
                continue
            key = self._build_notification_key(item["repo_info"], item["new_commits"])
            failed_payloads.append({
                "repo_info": item["repo_info"],
                "new_commits": item["new_commits"],
                "targets": item["failed_targets"],
                "group_targets": item["failed_group_targets"],
                # 标记来源为合并转发及其批次：重试时必须走合并转发发送器
                "forward_merge": True,
                "forward_batch": True,
                "branch": item["branch"],
                "key": self._build_notification_key(item["repo_info"], item["new_commits"]),
                "attempts": 1,
                "created_at": datetime.utcnow().isoformat(),
            })
            logger.warning(
                f"合并转发：{key} 部分目标发送失败"
                f"（私聊 {len(item['failed_targets'])} 个、群 {len(item['failed_group_targets'])} 个），已加入待重试队列"
            )

        if failed_payloads:
            failed_notifications = self._load_failed_notifications()
            failed_notifications.extend(failed_payloads)
            self._save_failed_notifications(failed_notifications)

        logger.info(f"合并转发：本轮 {len(pending)} 条通知处理完成，{len(failed_payloads)} 条存在发送失败目标")
        return pending

    async def _send_merged_forward(self, is_group: bool, target: str, items: List[Dict]):
        """向单个目标发送合并转发聊天记录。

        仅 aiocqhttp（OneBot v11）平台支持 Node/Nodes 合并转发；其他平台或当前
        AstrBot 版本不支持转发组件时，按 ``fallback_to_normal`` 决定降级为逐条
        普通消息，还是直接判定该目标发送失败。
        """
        target_str = str(target).strip()
        platform_name = self._resolve_platform_name(target_str)
        supported = platform_name == "aiocqhttp" and self._supports_forward_components()

        if not supported:
            if platform_name != "aiocqhttp":
                reason = f"平台 {platform_name or '未知'} 不支持合并转发"
            else:
                reason = "当前 AstrBot 版本不支持合并转发组件（Node/Nodes）"
            if not self.forward_fallback_to_normal:
                logger.warning(f"合并转发：{reason}，已跳过目标 {target_str}")
                self._mark_target_failed(is_group, target_str, items)
                return
            logger.info(f"合并转发：{reason}，目标 {target_str} 降级为逐条普通消息发送")
            if not await self._send_items_individually(is_group, target_str, items):
                self._mark_target_failed(is_group, target_str, items)
            return

        chains = await self._build_forward_chains(items)
        if not chains:
            logger.warning(f"合并转发：目标 {target_str} 没有可发送的节点内容，跳过")
            return

        for index, chain in enumerate(chains, 1):
            try:
                result = await self._send_forward_chain(target_str, is_group, chain)
            except Exception as e:
                logger.error(f"合并转发发送到 {target_str} 出错: {str(e)}", exc_info=True)
                result = {"success": False, "message": str(e)}
            if not result.get("success", False):
                logger.error(
                    f"合并转发发送到 {target_str} 失败（第 {index}/{len(chains)} 条）: "
                    f"{result.get('message') or '未知错误'}"
                )
                self._mark_target_failed(is_group, target_str, items)
                return

        logger.info(
            f"✅ 成功向 {target_str} 发送合并转发：{len(chains)} 条转发记录，"
            f"共 {len(items)} 个 commit 节点"
        )

    async def _retry_forward_notifications(self, entries: List[Dict]) -> List[Dict]:
        """重试合并转发模式下失败的目标（按目标重新合并成一个批次发送）

        每条待重试条目只保留了自己失败的目标；同一目标在多条条目里都可能出现，
        这里先按 (是否群聊, 目标) 归并回批次，再用合并转发发送器重发一次，
        使重试保持与首次发送一致的「一个目标一条转发记录」形态，而不是拆成多条
        普通消息。发送后仍失败的目标会被更新回各自的条目。

        Args:
            entries: 待重试的失败通知条目（含 forward_merge / forward_batch 元数据）。

        Returns:
            仍然失败、需要继续留在重试队列中的条目列表。
        """
        logger.info(f"合并转发：尝试重试 {len(entries)} 条失败通知的待重试目标")

        # 从磁盘恢复的条目没有 failed_* 字段（_normalize_failed_notifications 不落盘
        # 运行时状态），而发送失败时会由 _mark_target_failed 写入，这里先补齐。
        for entry in entries:
            entry["failed_targets"] = []
            entry["failed_group_targets"] = []

        # 保持条目顺序稳定，便于按目标还原原始节点顺序
        index_by_id = {id(entry): index for index, entry in enumerate(entries)}
        grouped: Dict[tuple, List[Dict]] = {}
        for entry in entries:
            for target in entry.get("targets", []):
                grouped.setdefault((False, target), []).append(entry)
            for target in entry.get("group_targets", []):
                grouped.setdefault((True, target), []).append(entry)

        for (is_group, target), items in grouped.items():
            items = sorted(items, key=lambda x: index_by_id.get(id(x), 0))
            try:
                await self._send_merged_forward(is_group, target, items)
            except Exception as e:
                logger.error(f"合并转发重试发送到 {target} 出错: {str(e)}", exc_info=True)
                self._mark_target_failed(is_group, target, items)

        still_failed: List[Dict] = []
        for entry in entries:
            failed_targets = self._merge_unique(entry.get("failed_targets", []), [])
            failed_group_targets = self._merge_unique(entry.get("failed_group_targets", []), [])
            if not failed_targets and not failed_group_targets:
                # 重试成功：与首次发送成功一致，标记为该批次已通知的目标
                self._mark_as_sent_in_main_record(
                    entry.get("repo_info", {}),
                    entry.get("new_commits", []),
                    entry.get("targets", []),
                    entry.get("group_targets", []),
                )
                continue
            entry["targets"] = failed_targets
            entry["group_targets"] = failed_group_targets
            entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
            still_failed.append(entry)

        logger.info(f"合并转发：重试后仍有 {len(still_failed)} 条通知存在失败目标")
        return still_failed

    @staticmethod
    def _mark_target_failed(is_group: bool, target: str, items: List[Dict]):
        """把发送失败的目标记入这些通知条目，供调用方写入待重试队列"""
        for item in items:
            key = "failed_group_targets" if is_group else "failed_targets"
            if target not in item[key]:
                item[key].append(target)

    async def _build_forward_chains(self, items: List[Dict]) -> List[MessageChain]:
        """把通知条目渲染为合并转发消息链，并按节点上限分片"""
        nodes = []
        for item in items:
            content = await self._build_forward_node_content(item)
            if not content:
                continue
            nodes.append(
                Comp.Node(
                    content=content,
                    name=self.forward_node_name,
                    uin=self.forward_node_uin,
                )
            )

        if not nodes:
            return []

        return [
            MessageChain(chain=[Comp.Nodes(nodes=chunk)])
            for chunk in (
                nodes[i:i + self.forward_max_nodes]
                for i in range(0, len(nodes), self.forward_max_nodes)
            )
        ]

    async def _build_forward_node_content(self, item: Dict) -> List:
        """构建单个转发节点的内容（文字或图片卡片），渲染失败时回退为文字节点"""
        if self._resolve_forward_content_mode() == "image":
            image_b64 = await self._render_commit_card_image(
                item["repo_info"], item["new_commits"], item.get("branch")
            )
            if image_b64:
                return [Comp.Image.fromBase64(image_b64)]
            logger.warning("合并转发：图片卡片渲染失败，该节点回退为文字消息")
        return [Comp.Plain(self._format_commit_message(item["repo_info"], item["new_commits"]))]

    def _resolve_forward_content_mode(self) -> str:
        """解析合并转发节点内容形式：follow 时跟随 commit_output_format"""
        if self.forward_content_mode == "follow":
            return "image" if self.commit_output_format == "image" else "text"
        return "image" if self.forward_content_mode == "image" else "text"

    async def _render_commit_card_image(self, repo_info: Dict, new_commits: List[Dict],
                                        branch: str = None) -> Optional[str]:
        """渲染 commit 图片卡片，返回 base64 字符串；渲染失败返回 None"""
        if not self.image_service:
            logger.error("文转图服务未注入，无法渲染 commit 卡片")
            return None
        try:
            return await self.image_service.render_commit_image(repo_info, new_commits, branch)
        except Exception as e:
            logger.error(f"渲染 commit 卡片图片失败: {str(e)}")
            return None

    async def _send_forward_chain(self, target: str, is_group: bool, message: MessageChain):
        """通过消息链通道发送合并转发（StarTools → 平台适配器 → OneBot 转发 API）"""
        session = self._parse_umo(target)
        if session is not None:
            return await self._send_by_session(session, message, target_desc=target)
        if is_group:
            return await self._send_group_message(target, message)
        return await self._send_private_message(target, message)

    async def _send_items_individually(self, is_group: bool, target: str, items: List[Dict]) -> bool:
        """降级路径：按原有逻辑把收集到的 commit 通知逐条发送给单个目标"""
        all_ok = True
        for item in items:
            temp_image_path = None
            try:
                message, image_b64, temp_image_path = await self._prepare_individual_payload(item)
                if message is None:
                    # 图片渲染失败：与原逻辑一致，计入失败交由重试队列处理
                    all_ok = False
                    continue
                result = await self._send_to_target(
                    target, is_group, message, image_b64, temp_image_path
                )
                if not result.get("success", False):
                    all_ok = False
            except Exception as e:
                logger.error(f"降级发送 commit 通知到 {target} 出错: {str(e)}", exc_info=True)
                all_ok = False
            finally:
                if temp_image_path:
                    try:
                        os.remove(temp_image_path)
                    except OSError:
                        pass
        return all_ok

    async def _prepare_individual_payload(self, item: Dict):
        """构建单条 commit 通知载荷（降级发送时使用）。

        Returns:
            (message, image_b64, temp_image_path)。文本模式为 (文本, None, None)；
            图片模式为 (消息链, base64, 临时文件路径)；图片渲染失败时首项为 None。
        """
        repo_info = item["repo_info"]
        new_commits = item["new_commits"]

        if self.commit_output_format != "image":
            return self._format_commit_message(repo_info, new_commits), None, None

        image_b64 = await self._render_commit_card_image(
            repo_info, new_commits, item.get("branch")
        )
        if not image_b64:
            return None, None, None

        temp_image_path = self._write_temp_image(image_b64)
        return self._build_image_chain(image_b64, temp_image_path), image_b64, temp_image_path

    def _resolve_platform_name(self, target: str) -> Optional[str]:
        """推断推送目标最终由哪种平台类型发送（如 aiocqhttp），未匹配到时返回 None"""
        target_str = str(target).strip()
        session = self._parse_umo(target_str)
        if session is not None:
            return self._get_platform_name_by_id(session.platform_name)
        if target_str.startswith("-"):
            # Telegram 群组目标（负号开头的 chat_id），按适配器类型确定平台
            return self._get_platform_name_by_id(
                self._get_platform_id(platform_type="telegram")
            )
        platform_id = self._get_platform_id(target=target_str)
        if not platform_id:
            return None
        return self._get_platform_name_by_id(platform_id)

    def _get_platform_name_by_id(self, platform_id: str) -> Optional[str]:
        """按平台实例 ID 查找平台类型名称（如 aiocqhttp）"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                if meta.id == platform_id:
                    return meta.name
        except Exception as e:
            logger.debug(f"查找平台类型时出错: {str(e)}")
        return None

    @staticmethod
    def _supports_forward_components() -> bool:
        """当前 AstrBot 版本是否提供合并转发组件（Node/Nodes）"""
        return (getattr(Comp, "Node", None) is not None
                and getattr(Comp, "Nodes", None) is not None)

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
                image_b64 = await self._render_commit_card_image(repo_info, new_commits, branch)
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

    def _split_text_chain(self, message_chain: MessageChain) -> List[MessageChain]:
        """把超长的纯文本消息链拆分为多条，其他类型消息链（图文/图片等）原样返回"""
        try:
            if len(message_chain.chain) == 1 and isinstance(message_chain.chain[0], Comp.Plain):
                text = message_chain.chain[0].text or ""
                chunks = split_long_message(text, self.message_max_length)
                if len(chunks) > 1:
                    logger.info(
                        f"消息长度 {len(text)} 超过安全长度 {self.message_max_length}，"
                        f"已拆分为 {len(chunks)} 条依次发送"
                    )
                    return [MessageChain(chain=[Comp.Plain(c)]) for c in chunks]
        except Exception as e:
            logger.warning(f"拆分长消息时出错，按原消息发送: {str(e)}")
        return [message_chain]

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
            return await self._send_by_session(session, message, target_desc=user_id_str)
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
            return await self._send_by_session(session, message, target_desc=group_id_str)
        except Exception as e:
            error_msg = f"发送群消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
