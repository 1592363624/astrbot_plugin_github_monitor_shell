import json
import os
from typing import List, Dict

from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.star import StarTools


class NotificationService:
    def __init__(self, context):
        self.context = context
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.failed_notifications_file = os.path.join(plugin_data_dir, "failed_notifications.json")
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        data_dir = os.path.dirname(self.failed_notifications_file)
        os.makedirs(data_dir, exist_ok=True)

    def _load_failed_notifications(self) -> List:
        """加载发送失败的通知"""
        try:
            if os.path.exists(self.failed_notifications_file):
                with open(self.failed_notifications_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 清理过期的通知数据（比如仓库已删除的通知）
                    valid_notifications = [n for n in data if self._is_notification_valid(n)]
                    if len(valid_notifications) != len(data):
                        self._save_failed_notifications(valid_notifications)
                    return valid_notifications
            return []
        except Exception as e:
            logger.error(f"加载失败通知记录失败: {str(e)}")
            return []

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
                json.dump(notifications, f, ensure_ascii=False, indent=2)
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
            success = await self._send_notification(
                notification["repo_info"],
                notification["new_commits"],
                notification["targets"],
                notification["group_targets"]
            )

            if not success:
                remaining_notifications.append(notification)

        # 保存仍然失败的通知
        self._save_failed_notifications(remaining_notifications)
        logger.info(f"重试后仍失败的通知数量: {len(remaining_notifications)}")

    async def send_commit_notification(self, repo_info: Dict, new_commits: List[Dict], targets: List[str],
                                       group_targets: List[str] = None):
        """发送commit变更通知"""
        # 检查是否有有效的提交
        if not new_commits:
            logger.info("没有新的提交需要通知")
            return

        try:
            success = await self._send_notification(repo_info, new_commits, targets, group_targets)

            # 如果发送失败，保存到失败列表中
            if not success:
                failed_notifications = self._load_failed_notifications()
                failed_notifications.append({
                    "repo_info": repo_info,
                    "new_commits": new_commits,
                    "targets": targets,
                    "group_targets": group_targets
                })
                self._save_failed_notifications(failed_notifications)
                logger.warning("通知发送失败，已保存到待重试列表")
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")
            # 保存到失败列表中
            try:
                failed_notifications = self._load_failed_notifications()
                failed_notifications.append({
                    "repo_info": repo_info,
                    "new_commits": new_commits,
                    "targets": targets,
                    "group_targets": group_targets
                })
                self._save_failed_notifications(failed_notifications)
                logger.warning("通知发送异常，已保存到待重试列表")
            except Exception as save_error:
                logger.error(f"保存失败通知记录也失败了: {str(save_error)}")

    async def _send_notification(self, repo_info: Dict, new_commits: List[Dict], targets: List[str],
                                 group_targets: List[str] = None) -> bool:
        """实际发送通知"""
        try:
            message = self._format_commit_message(repo_info, new_commits)

            success = True

            # 发送私聊消息
            for target in targets:
                if target:  # 确保目标不为空
                    result = await self._send_private_message(int(target), message)
                    if not result.get("success", False):
                        success = False

            # 发送群消息
            if group_targets:
                for group_target in group_targets:
                    if group_target:  # 确保目标不为空
                        result = await self._send_group_message(int(group_target), message)
                        if not result.get("success", False):
                            success = False

            return success
        except Exception as e:
            logger.error(f"发送通知时发生异常: {str(e)}")
            return False

    def _format_commit_message(self, repo_info: Dict, new_commits: List[Dict]) -> str:
        """格式化commit消息"""
        repo_name = f"{repo_info['owner']['login']}/{repo_info['name']}"

        message = f"🔔 GitHub仓库更新通知\n\n"
        message += f"📁 仓库: {repo_name}\n"
        message += f"🔗 链接: {repo_info['html_url']}\n\n"

        if len(new_commits) == 1:
            # 只有一个提交的向后兼容格式
            commit = new_commits[0]
            message += f"✨ 新Commit:\n"
            message += f"📝 SHA: {commit['sha'][:7]}\n"
            message += f"👤 作者: {commit['author']}\n"
            message += f"📅 时间: {commit['date']}\n"
            message += f"💬 信息: {commit['message']}\n"
            message += f"🔗 链接: {commit['url']}\n\n"
        else:
            # 有多个提交的格式
            message += f"✨ 本次更新包含 {len(new_commits)} 个新提交:\n\n"
            for i, commit in enumerate(new_commits, 1):
                message += f"{i}. 提交 SHA: {commit['sha'][:7]}\n"
                message += f"   作者: {commit['author']}\n"
                message += f"   时间: {commit['date']}\n"
                message += f"   信息: {commit['message']}\n"
                message += f"   链接: {commit['url']}\n\n"

        return message

    async def _send_private_message(self, user_id: int, message: str):
        """通过 AstrBot 通用接口主动发送私聊消息"""
        try:
            user_id_str = str(user_id)
            if not user_id_str.isdigit():
                error_msg = f"发送私聊消息失败: 非法的QQ号:{user_id_str}"
                logger.error(error_msg)
                return {"success": False, "message": error_msg}
            message_chain = MessageChain().message(message)
            await StarTools.send_message_by_id(
                type="PrivateMessage",
                id=user_id_str,
                message_chain=message_chain,
            )
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
                await StarTools.send_message_by_id(
                    type="GroupMessage",
                    id=group_id_str,
                    message_chain=message_chain,
                )
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
                await StarTools.send_message(session, message_chain)
                logger.info(f"✅ 成功向 Telegram 群 {group_id_str} 发送消息")
                return {"success": True}

            error_msg = f"发送群消息失败: 非法的群标识:{group_id_str}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"发送群消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
