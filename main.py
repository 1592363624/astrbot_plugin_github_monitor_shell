import asyncio
import json
import os
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools
from .services.github_service import GitHubService
from .services.notification_service import NotificationService


# 移除了 global_vars 的导入


@register("GitHub监控插件", "Shell", "定时监控GitHub仓库commit变化并发送通知", "1.0.4",
          "https://github.com/1592363624/astrbot_plugin_github_monitor_shell")
class GitHubMonitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.github_service = GitHubService(self.config.get("github_token", ""))
        self.notification_service = NotificationService(context)
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.data_file = os.path.join(plugin_data_dir, "commits.json")
        self.bot_instance = None  # 将全局变量改为类实例变量
        self.monitoring_started = False  # 添加标志以跟踪监控是否已启动
        self._ensure_data_dir()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def _capture_bot_instance(self, event: AstrMessageEvent):
        """捕获机器人实例用于后台任务"""

        if self.bot_instance is None and event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    self.bot_instance = event.bot
                    self.platform_name = "aiocqhttp"
                    logger.info("成功捕获 aiocqhttp 机器人实例，后台 API 调用已启用。")
                    # 在捕获到 bot_instance 后启动监控
                    self._start_monitoring()
                    # 重试之前失败的通知
                    await self.notification_service.retry_failed_notifications()
            except ImportError:
                logger.warning("无法导入 AiocqhttpMessageEvent，后台 API 调用可能受限。")

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        data_dir = os.path.dirname(self.data_file)
        os.makedirs(data_dir, exist_ok=True)

    def _load_commit_data(self) -> Dict:
        """加载commit数据"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载commit数据失败: {str(e)}")
            return {}

    def _save_commit_data(self, data: Dict):
        """保存commit数据"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存commit数据失败: {str(e)}")

    def _start_monitoring(self):
        """启动监控任务"""
        # 只启动一次监控任务
        if not self.monitoring_started:
            asyncio.create_task(self._monitor_loop())
            self.monitoring_started = True
            logger.info("GitHub 监控任务已启动")

    async def _monitor_loop(self):
        """监控循环"""
        while True:
            try:
                await self._check_repositories()
                # 定期重试失败的通知
                await self.notification_service.retry_failed_notifications()
                await asyncio.sleep(self.config.get("check_interval", 30) * 60)
            except Exception as e:
                logger.error(f"监控循环出错: {str(e)}")
                await asyncio.sleep(60)  # 出错时等待1分钟再重试

    async def _check_repositories(self):
        """检查所有仓库的更新"""
        repositories = self.config.get("repositories", [])
        if not repositories:
            return

        commit_data = self._load_commit_data()
        notification_targets = self.config.get("notification_targets", [])

        for repo_config in repositories:
            # 支持新的仓库配置格式，可以在仓库后指定群号
            # 字符串格式: "owner/repo|group1|group2|..."
            # 字典格式: {"owner": "...", "repo": "...", "groups": [...], ...}
            extra_groups = []
            if isinstance(repo_config, str):
                parts = repo_config.split("|")
                owner, repo = parts[0].split("/", 1)
                branch = None  # 不指定分支，使用默认分支
                if len(parts) > 1:
                    extra_groups = parts[1:]  # 提取额外的群号
            elif isinstance(repo_config, dict):
                owner = repo_config.get("owner")
                repo = repo_config.get("repo")
                branch = repo_config.get("branch")  # 如果没有指定分支，会使用默认分支
                extra_groups = repo_config.get("groups", [])  # 获取该仓库专用的群号列表
            else:
                logger.warning(f"无效的仓库配置: {repo_config}")
                continue

            if not owner or not repo:
                logger.warning(f"仓库配置缺少owner或repo: {repo_config}")
                continue

            # 获取最新commit
            new_commit = await self.github_service.get_latest_commit(owner, repo, branch)
            if not new_commit:
                continue

            # 使用仓库的实际默认分支名作为key的一部分
            repo_info = await self.github_service.get_repository_info(owner, repo)
            default_branch = repo_info.get("default_branch", "main") if repo_info else "main"
            actual_branch = branch if branch else default_branch
            repo_key = f"{owner}/{repo}/{actual_branch}"

            old_commit = commit_data.get(repo_key)

            # 检查是否有变化
            if not old_commit or old_commit.get("sha") != new_commit["sha"]:
                logger.info(f"检测到仓库 {repo_key} 有新的commit: {new_commit['sha'][:7]}")

                # 获取仓库信息
                if not repo_info:
                    repo_info = await self.github_service.get_repository_info(owner, repo)

                # 获取所有新的提交
                new_commits = [new_commit]  # 默认至少包含最新提交
                if old_commit and old_commit.get("sha"):
                    # 获取从上次记录的提交之后的所有提交
                    commits_since = await self.github_service.get_commits_since(
                        owner, repo, old_commit.get("sha"), branch)
                    if commits_since:
                        new_commits = commits_since

                # 发送通知
                if repo_info:
                    # 合并全局群通知目标和该仓库专用的群通知目标
                    global_groups = self.config.get("group_notification_targets", [])
                    all_groups = list(set(global_groups + extra_groups))  # 去重合并
                    await self.notification_service.send_commit_notification(
                        repo_info, new_commits, notification_targets, all_groups
                    )

                # 更新数据
                commit_data[repo_key] = new_commit  # 仍然只保存最新的提交SHA用于比较
                self._save_commit_data(commit_data)

    @filter.command("github_monitor")
    async def monitor_command(self, event: AstrMessageEvent):
        """手动触发监控检查"""
        try:
            await self._check_repositories()
            yield event.plain_result("✅ 已完成GitHub仓库检查")
        except Exception as e:
            logger.error(f"手动检查失败: {str(e)}")
            yield event.plain_result(f"❌ 检查失败: {str(e)}")

    @filter.command("github_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看监控状态"""
        try:
            commit_data = self._load_commit_data()
            repositories = self.config.get("repositories", [])

            message = "📊 GitHub监控状态\n\n"

            for repo_config in repositories:
                if isinstance(repo_config, str):
                    owner, repo = repo_config.split("/", 1)
                    # 获取仓库信息以确定默认分支
                    repo_info = await self.github_service.get_repository_info(owner, repo)
                    default_branch = repo_info.get("default_branch", "main") if repo_info else "main"
                    branch = default_branch
                elif isinstance(repo_config, dict):
                    owner = repo_config.get("owner")
                    repo = repo_config.get("repo")
                    branch = repo_config.get("branch")
                    if (not owner) or (not repo):
                        continue
                    # 如果没有指定分支，获取默认分支
                    if not branch:
                        repo_info = await self.github_service.get_repository_info(owner, repo)
                        branch = repo_info.get("default_branch", "main") if repo_info else "main"
                else:
                    continue

                repo_key = f"{owner}/{repo}/{branch}"
                commit_info = commit_data.get(repo_key)

                message += f"📁 {repo_key}\n"
                if commit_info:
                    message += f"  最新Commit: {commit_info['sha'][:7]}\n"
                    message += f"  更新时间: {commit_info['date']}\n"
                else:
                    message += f"  状态: 未监控到数据\n"
                message += "\n"

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"获取状态失败: {str(e)}")
            yield event.plain_result(f"❌ 获取状态失败: {str(e)}")