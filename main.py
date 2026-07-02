import asyncio
import json
import os
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools
from .services.github_service import GitHubService
from .services.notification_service import NotificationService, format_commit_datetime

class GitHubMonitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.github_service = GitHubService(self.config.get("github_token", ""))
        self.notification_service = NotificationService(context, self.config)
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.data_file = os.path.join(plugin_data_dir, "commits.json")
        self.sent_notifications_file = os.path.join(plugin_data_dir, "sent_notifications.json")
        self.monitoring_started = False  # 添加标志以跟踪监控是否已启动
        self._monitor_task: asyncio.Task | None = None
        self._ensure_data_dir()
        self._start_monitoring()

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

    def _load_sent_notifications(self) -> Dict:
        """加载已发送通知记录"""
        try:
            if os.path.exists(self.sent_notifications_file):
                with open(self.sent_notifications_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载已发送通知记录失败: {str(e)}")
            return {}

    def _save_sent_notifications(self, data: Dict):
        """保存已发送通知记录"""
        try:
            with open(self.sent_notifications_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存已发送通知记录失败: {str(e)}")

    def _is_commit_already_notified(self, repo_key: str, commit_sha: str, groups: List[str]) -> bool:
        """检查commit是否已经发送过通知给这些群组"""
        sent_data = self._load_sent_notifications()
        repo_data = sent_data.get(repo_key, {})
        commit_data = repo_data.get(commit_sha, [])
        return any(set(groups) <= set(g) for g in commit_data)

    def _mark_commit_as_notified(self, repo_key: str, commit_sha: str, groups: List[str]):
        """标记commit已发送通知"""
        sent_data = self._load_sent_notifications()
        if repo_key not in sent_data:
            sent_data[repo_key] = {}
        if commit_sha not in sent_data[repo_key]:
            sent_data[repo_key][commit_sha] = []
        sent_data[repo_key][commit_sha].append(list(set(groups)))
        self._save_sent_notifications(sent_data)

    def _start_monitoring(self):
        """启动监控任务"""
        # 只启动一次监控任务
        if not self.monitoring_started:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            self.monitoring_started = True
            logger.info("GitHub 监控任务已启动")

    async def terminate(self):
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"终止监控任务时出错: {str(e)}")
        self.monitoring_started = False
        self._monitor_task = None

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
        
        # 创建当前配置中的仓库键集合，用于清理已删除的仓库数据
        configured_repo_keys = set()

        for repo_config in repositories:
            # 支持新的仓库配置格式，可以在仓库后指定群号
            # 字符串格式: "owner/repo|group1|group2|..."
            # 字典格式: {"owner": "...", "repo": "...", "groups": [...], ...}
            extra_groups = []
            if isinstance(repo_config, str):
                # 分离仓库路径和群号
                parts = repo_config.split("|")
                repo_path = parts[0]
                if "/" not in repo_path:
                    logger.warning(f"无效的仓库路径格式: {repo_config}")
                    continue
                owner, repo = repo_path.split("/", 1)
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

            # 获取仓库信息以确定实际分支
            repo_info = await self.github_service.get_repository_info(owner, repo)
            if not repo_info:
                logger.warning(f"无法获取仓库信息: {owner}/{repo}")
                continue
                
            default_branch = repo_info.get("default_branch", "main") if repo_info else "main"
            actual_branch = branch if branch else default_branch
            repo_key = f"{owner}/{repo}/{actual_branch}"
            
            # 将当前仓库键添加到配置集合中
            configured_repo_keys.add(repo_key)

            # 获取最新commit
            new_commit = await self.github_service.get_latest_commit(owner, repo, branch)
            if not new_commit:
                continue

            old_commit = commit_data.get(repo_key)

            # 检查是否有变化
            if not old_commit or old_commit.get("sha") != new_commit["sha"]:
                logger.info(f"检测到仓库 {repo_key} 有新的commit: {new_commit['sha'][:7]}")

                # 获取所有新的提交
                new_commits = [new_commit]  # 默认至少包含最新提交
                if old_commit and old_commit.get("sha"):
                    # 获取从上次记录的提交之后的所有提交
                    commits_since = await self.github_service.get_commits_since(
                        owner, repo, old_commit.get("sha"), branch)
                    if commits_since is not None:
                        # 如果获取到了提交列表（可能为空），使用获取到的列表
                        # 如果为空列表，说明没有新提交，但new_commit已经包含最新提交
                        if commits_since:
                            new_commits = commits_since
                        # 如果commits_since为空列表，保持new_commits = [new_commit]
                    else:
                        # API调用失败，跳过此仓库，但保留旧数据不变
                        continue

                # 发送通知 (只有在确实有新提交时才发送)
                if repo_info and new_commits:
                    # 合并全局群通知目标和该仓库专用的群通知目标
                    global_groups = self.config.get("group_notification_targets", [])
                    all_groups = list(set(global_groups + extra_groups))  # 去重合并

                    # 检查是否已经发送过通知
                    latest_sha = new_commits[0]["sha"]
                    if self._is_commit_already_notified(repo_key, latest_sha, all_groups):
                        logger.info(f"仓库 {repo_key} 的提交 {latest_sha[:7]} 已经发送过通知，跳过")
                    else:
                        # 发送通知
                        await self.notification_service.send_commit_notification(
                            repo_info, new_commits, notification_targets, all_groups
                        )
                        # 标记为已发送
                        self._mark_commit_as_notified(repo_key, latest_sha, all_groups)
                        logger.info(f"已标记仓库 {repo_key} 的提交 {latest_sha[:7]} 为已通知")

                # 更新数据
                commit_data[repo_key] = new_commit  # 仍然只保存最新的提交SHA用于比较
                self._save_commit_data(commit_data)
                
        # 清理已删除仓库的数据
        removed_keys = set(commit_data.keys()) - configured_repo_keys
        for removed_key in removed_keys:
            del commit_data[removed_key]
            logger.info(f"已清理已删除仓库的数据: {removed_key}")
        if removed_keys:
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
                    # 正确处理带群号的仓库配置
                    parts = repo_config.split("|")
                    repo_path = parts[0]
                    if "/" not in repo_path:
                        continue
                    owner, repo = repo_path.split("/", 1)
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
                    date_str = commit_info.get("date")
                    formatted_date = None
                    if date_str:
                        formatted_date = format_commit_datetime(
                            date_str,
                            self.config.get("time_zone", "Asia/Shanghai"),
                            self.config.get("time_format", "%Y-%m-%d %H:%M:%S"),
                        )

                    message += f"  最新Commit: {commit_info['sha'][:7]}\n"
                    if formatted_date:
                        message += f"  更新时间: {formatted_date}\n"
                    else:
                        message += f"  更新时间: 未知\n"
                else:
                    message += f"  状态: 未监控到数据\n"
                message += "\n"

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"获取状态失败: {str(e)}")
            yield event.plain_result(f"❌ 获取状态失败: {str(e)}")

    @filter.command("github_issues")
    async def issues_command(self, event: AstrMessageEvent):
        """查询当前用户所有仓库的 open issues（需要配置 github_token）"""
        try:
            # 检查是否配置了 token
            if not self.config.get("github_token"):
                yield event.plain_result("⚠️ 请先在插件配置中填写 github_token，否则无法获取你的仓库列表")
                return

            # 获取当前认证用户信息
            user = await self.github_service.get_current_user()
            if not user:
                yield event.plain_result("❌ 无法获取用户信息，请检查 github_token 是否有效")
                return

            username = user["login"]

            # 分页获取用户所有仓库（/user/repos 认证接口，含私有仓库，type=owner 不含 fork）
            all_repos = []
            page = 1
            while True:
                repos = await self.github_service.get_user_repos(page=page, per_page=100)
                if repos is None:
                    yield event.plain_result(f"❌ 获取用户 {username} 的仓库列表失败")
                    return
                if not repos:
                    break
                all_repos.extend(repos)
                if len(repos) < 100:
                    break
                page += 1

            if not all_repos:
                yield event.plain_result(f"✅ 用户 {username} 没有任何仓库")
                return

            message = f"📋 {username} 的 Open Issues\n\n"
            total_issues = 0
            repos_with_issues = 0

            for repo in all_repos:
                repo_name = repo["full_name"]

                # 跳过没有 open issues 的仓库（利用 API 返回的计数快速过滤）
                if repo.get("open_issues_count", 0) == 0:
                    continue

                # 获取该仓库的 open issues 详情
                issues = await self.github_service.get_open_issues(repo["owner"]["login"], repo["name"])
                if not issues:
                    continue

                # 确认有 issues 后才计数，避免计数与实际不一致
                repos_with_issues += 1
                message += f"📁 {repo_name}（{len(issues)} 个 open issues）\n"

                for issue in issues:
                    labels_str = ""
                    if issue["labels"]:
                        labels_str = f" 🏷️ {','.join(issue['labels'])}"
                    message += f"  #{issue['number']} {issue['title']}{labels_str}\n"
                    message += f"     👤 {issue['author']} | 🔗 {issue['url']}\n"
                    total_issues += 1

                message += "\n"

            if repos_with_issues == 0:
                yield event.plain_result(f"✅ {username} 的所有仓库均无 open issues（共 {len(all_repos)} 个仓库）")
            else:
                message += f"📊 共 {len(all_repos)} 个仓库，其中 {repos_with_issues} 个仓库有 open issues，共 {total_issues} 个"
                yield event.plain_result(message)

        except Exception as e:
            logger.error(f"查询 issues 失败: {str(e)}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")
