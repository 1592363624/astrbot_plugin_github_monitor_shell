from typing import List, Dict

from astrbot.api import logger


class NotificationService:
    def __init__(self, context):
        self.context = context

    async def send_commit_notification(self, repo_info: Dict, new_commit: Dict, old_commit: Dict, targets: List[str]):
        """发送commit变更通知"""
        try:
            message = self._format_commit_message(repo_info, new_commit, old_commit)

            for target in targets:
                await self._send_private_message(int(target), message)

        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")

    def _format_commit_message(self, repo_info: Dict, new_commit: Dict, old_commit: Dict) -> str:
        """格式化commit消息"""
        repo_name = f"{repo_info['owner']['login']}/{repo_info['name']}"

        message = f"🔔 GitHub仓库更新通知\n\n"
        message += f"📁 仓库: {repo_name}\n"
        message += f"🔗 链接: {repo_info['html_url']}\n\n"
        message += f"✨ 新Commit:\n"
        message += f"📝 SHA: {new_commit['sha'][:7]}\n"
        message += f"👤 作者: {new_commit['author']}\n"
        message += f"📅 时间: {new_commit['date']}\n"
        message += f"💬 信息: {new_commit['message'][:50]}{'...' if len(new_commit['message']) > 50 else ''}\n"
        message += f"🔗 链接: {new_commit['url']}\n\n"

        if old_commit:
            message += f"📜 之前Commit: {old_commit['sha'][:7]}\n"

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
                logger.error("❌ bot 实例未捕获，无法发送私聊消息。")
                return {"success": False, "message": "未捕获 bot 实例"}

            # 直接调用 NapCat API（底层同 /send_private_msg）
            result = await github_plugin.bot_instance.api.call_action(
                "send_private_msg",
                user_id=user_id,
                message=message
            )
            logger.info(f"✅ 成功向 {user_id} 发送私聊消息: {message}")
            return {"success": True, "result": result}

        except Exception as e:
            error_msg = f"发送私聊消息失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
