import json
import os
from typing import List, Dict

from astrbot.api import logger
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
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"加载失败通知记录失败: {str(e)}")
            return []

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
                result = await self._send_private_message(int(target), message)
                if not result.get("success", False):
                    success = False

            # 发送群消息
            if group_targets:
                for group_target in group_targets:
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
        """通过捕获的 NapCat bot 实例主动发送私聊消息"""
        try:
            # 获取插件实例来访问 bot_instance
            github_plugin = None
            # 通过 context 获取所有插件，然后找到我们的插件
            for star in self.context.get_all_stars():
                if star.name == "GitHub监控插件":
                    github_plugin = star.star_cls
                    break

            if not github_plugin or not github_plugin.bot_instance:
                logger.warning("❌ bot 实例未捕获，无法发送私聊消息。")
                return {"success": False, "message": "未捕获 bot 实例"}

            # 直接调用 NapCat API（底层同 /send_private_msg）
            result = await github_plugin.bot_instance.api.call_action(
                "send_private_msg",
                user_id=user_id,
                message=message
            )
            logger.info(f"✅ 成功向 {user_id} 发送私聊消息")
            return {"success": True, "result": result}

        except Exception as e:
            error_msg = f"发送私聊消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    async def _send_group_message(self, group_id: int, message: str):
        """通过捕获的 NapCat bot 实例主动发送群消息"""
        try:
            # 获取插件实例来访问 bot_instance
            github_plugin = None
            # 通过 context 获取所有插件，然后找到我们的插件
            for star in self.context.get_all_stars():
                if star.name == "GitHub监控插件":
                    github_plugin = star.star_cls
                    break

            if not github_plugin or not github_plugin.bot_instance:
                logger.warning("❌ bot 实例未捕获，无法发送群消息。")
                return {"success": False, "message": "未捕获 bot 实例"}

            # 直接调用 NapCat API（底层同 /send_group_msg）
            result = await github_plugin.bot_instance.api.call_action(
                "send_group_msg",
                group_id=group_id,
                message=message
            )
            logger.info(f"✅ 成功向群 {group_id} 发送消息")
            return {"success": True, "result": result}

        except Exception as e:
            error_msg = f"发送群消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}