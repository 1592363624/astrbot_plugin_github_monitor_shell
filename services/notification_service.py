import json
import os
from datetime import datetime
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star import StarTools


def format_commit_datetime(
    date_str: str,
    time_zone: str,
    time_format: str,
) -> Optional[str]:
    try:
        normalized = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        target_tz = ZoneInfo(time_zone)
        return dt.astimezone(target_tz).strftime(time_format)
    except Exception:
        return None


class NotificationService:
    def __init__(self, context, config: Dict | None = None):
        self.context = context
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.failed_notifications_file = os.path.join(plugin_data_dir, "failed_notifications.json")
        self.time_zone = (config or {}).get("time_zone", "Asia/Shanghai")
        self.time_format = (config or {}).get("time_format", "%Y-%m-%d %H:%M:%S")
        # 从配置中获取平台ID，如果未配置则自动查找aiocqhttp平台
        self.platform_id = (config or {}).get("platform_id", None)
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        data_dir = os.path.dirname(self.failed_notifications_file)
        os.makedirs(data_dir, exist_ok=True)

    def _get_platform_id(self, platform_type: str = "aiocqhttp") -> Optional[str]:
        """获取平台的ID

        Args:
            platform_type: 平台类型名称，如 "aiocqhttp", "telegram"

        Returns:
            平台的ID，如果未找到则返回None
        """
        # 如果配置了platform_id，直接返回
        if self.platform_id:
            return self.platform_id

        # 自动查找指定类型的平台
        for platform in self.context.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.name == platform_type:
                return meta.id
        return None

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
                                       group_targets: List[str] = None):
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
            )

            if failed_targets or failed_group_targets:
                failed_notifications = self._load_failed_notifications()
                failed_notifications.append(
                    {
                        "repo_info": repo_info,
                        "new_commits": new_commits,
                        "targets": failed_targets,
                        "group_targets": failed_group_targets,
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
    ) -> tuple[List[str], List[str]]:
        try:
            message = self._format_commit_message(repo_info, new_commits)
            failed_targets: List[str] = []
            failed_group_targets: List[str] = []

            for target in self._merge_unique(self._normalize_target_list(targets), []):
                try:
                    result = await self._send_private_message(int(target), message)
                    if not result.get("success", False):
                        failed_targets.append(target)
                except Exception:
                    failed_targets.append(target)

            for group_target in self._merge_unique(self._normalize_target_list(group_targets), []):
                try:
                    result = await self._send_group_message(int(group_target), message)
                    if not result.get("success", False):
                        failed_group_targets.append(group_target)
                except Exception:
                    failed_group_targets.append(group_target)

            return failed_targets, failed_group_targets
        except Exception as e:
            logger.error(f"发送通知时发生异常: {str(e)}")
            return self._normalize_target_list(targets), self._normalize_target_list(group_targets)

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

    async def _send_private_message(self, user_id: int, message: str):
        """通过 AstrBot 通用接口主动发送私聊消息

        使用 MessageSession 构造会话对象，通过 StarTools.send_message 发送消息。
        """
        try:
            user_id_str = str(user_id)
            if not user_id_str.isdigit():
                error_msg = f"发送私聊消息失败: 非法的QQ号:{user_id_str}"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            # 获取平台ID
            platform_id = self._get_platform_id("aiocqhttp")
            if not platform_id:
                error_msg = "发送私聊消息失败: 未找到aiocqhttp平台，请检查平台是否已启动或在配置中指定platform_id"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            # 构造私聊会话对象
            session = MessageSesion(
                platform_name=platform_id,
                message_type=MessageType.FRIEND_MESSAGE,
                session_id=user_id_str,
            )
            message_chain = MessageChain().message(message)
            sent = await StarTools.send_message(session, message_chain)

            if not sent:
                error_msg = f"发送私聊消息失败: 找不到平台 {platform_id}，请检查平台是否已启动"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}

            logger.info(f"✅ 成功向 {user_id} 发送私聊消息")
            return {"success": True}
        except Exception as e:
            error_msg = f"发送私聊消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    async def _send_group_message(self, group_id: int, message: str):
        """通过 AstrBot 通用接口主动发送群消息"""
        try:
            group_id_str = str(group_id)
            message_chain = MessageChain().message(message)

            if group_id_str.isdigit():
                # 获取平台ID
                platform_id = self._get_platform_id("aiocqhttp")
                if not platform_id:
                    error_msg = "发送群消息失败: 未找到aiocqhttp平台，请检查平台是否已启动或在配置中指定platform_id"
                    logger.error(error_msg)
                    return {"success": False, "message": error_msg}

                # 构造 QQ 群会话对象
                session = MessageSesion(
                    platform_name=platform_id,
                    message_type=MessageType.GROUP_MESSAGE,
                    session_id=group_id_str,
                )
                sent = await StarTools.send_message(session, message_chain)
                if not sent:
                    error_msg = f"发送群消息失败: 找不到平台 {platform_id}，请检查平台是否已启动"
                    logger.error(error_msg)
                    return {"success": False, "message": error_msg}
                logger.info(f"✅ 成功向 QQ 群 {group_id_str} 发送消息")
                return {"success": True}

            if group_id_str.startswith("-"):
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
                sent = await StarTools.send_message(session, message_chain)
                if not sent:
                    error_msg = f"发送群消息失败: 找不到平台 {platform_id}"
                    logger.error(error_msg)
                    return {"success": False, "message": error_msg}
                logger.info(f"✅ 成功向 Telegram 群 {group_id_str} 发送消息")
                return {"success": True}

            error_msg = f"发送群消息失败: 非法的群标识:{group_id_str}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"发送群消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
