import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star import StarTools
from .services.github_service import GitHubService
from .services.notification_service import NotificationService, format_commit_datetime
from .utils.cron_utils import cron_matches, get_next_run_time


@register("GitHub监控插件", "Shell", "定时监控GitHub仓库commit变化并发送通知", "1.2.6",
          "https://github.com/1592363624/astrbot_plugin_github_monitor_shell")
class GitHubMonitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.github_service = GitHubService(self.config.get("github_token", ""))
        self.notification_service = NotificationService(context, self.config)
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        self.data_file = os.path.join(plugin_data_dir, "commits.json")
        self.sent_notifications_file = os.path.join(plugin_data_dir, "sent_notifications.json")
        self.issues_snapshot_file = os.path.join(plugin_data_dir, "issues_snapshot.json")
        self.issues_push_log_file = os.path.join(plugin_data_dir, "issues_push_log.json")
        self.monitoring_started = False  # 添加标志以跟踪监控是否已启动
        self._monitor_task: asyncio.Task | None = None
        self._issues_cron_task: asyncio.Task | None = None  # Issues 定时推送任务
        self._ensure_data_dir()
        self._start_monitoring()
        self._start_issues_cron_task()

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

    def _load_issues_snapshot(self) -> Dict:
        """加载上次 issues 快照（用于对比变化）"""
        try:
            if os.path.exists(self.issues_snapshot_file):
                with open(self.issues_snapshot_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载 issues 快照失败: {str(e)}")
            return {}

    def _save_issues_snapshot(self, data: Dict):
        """保存 issues 快照"""
        try:
            with open(self.issues_snapshot_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 issues 快照失败: {str(e)}")

    def _load_issues_push_log(self) -> Dict:
        """加载推送日志（记录上次推送时间，用于间隔保护）"""
        try:
            if os.path.exists(self.issues_push_log_file):
                with open(self.issues_push_log_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载 issues 推送日志失败: {str(e)}")
            return {}

    def _save_issues_push_log(self, data: Dict):
        """保存推送日志"""
        try:
            with open(self.issues_push_log_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 issues 推送日志失败: {str(e)}")

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

    def _start_issues_cron_task(self):
        """启动 Issues 定时推送任务（根据 cron 表达式）"""
        if not self.config.get("issues_cron_enabled", False):
            logger.info("Issues 定时推送未启用")
            return

        cron_expr = self.config.get("issues_cron_expression", "0 9 * * *")
        run_desc = get_next_run_time(cron_expr, self.config.get("time_zone", "Asia/Shanghai"))
        logger.info(f"Issues 定时推送已启动，Cron: {cron_expr}（{run_desc}）")
        self._issues_cron_task = asyncio.create_task(self._issues_cron_loop())

    async def _issues_cron_loop(self):
        """Issues 定时推送循环：每分钟检查一次是否匹配 cron 表达式"""
        cron_expr = self.config.get("issues_cron_expression", "0 9 * * *")
        time_zone = self.config.get("time_zone", "Asia/Shanghai")
        notification_targets = self.config.get("notification_targets", [])
        group_targets = self.config.get("group_notification_targets", [])

        while True:
            try:
                now = datetime.now(ZoneInfo("UTC"))
                if cron_matches(cron_expr, now, time_zone):
                    logger.info(f"触发 Issues 定时推送（Cron: {cron_expr}）")
                    await self._send_issues_notification(notification_targets, group_targets)

                # 每分钟检查一次
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Issues 定时推送循环出错: {str(e)}")
                await asyncio.sleep(60)

    async def _send_issues_notification(self, private_targets: List[str], group_targets: List[str] = None):
        """查询所有仓库的 open issues，对比快照后推送变化内容

        优化点：
        1. 支持私聊 + 群聊双通道推送
        2. 与上次快照对比，只推送新增/更新的 issue
        3. 推送间隔保护：同一批 issue 不会在短时间内重复推送
        """
        if group_targets is None:
            group_targets = []

        try:
            if not self.config.get("github_token"):
                logger.warning("Issues 定时推送：未配置 github_token，跳过")
                return

            # 获取当前认证用户信息
            user = await self.github_service.get_current_user()
            if not user:
                logger.error("Issues 定时推送：无法获取用户信息")
                return

            username = user["login"]

            # 分页获取用户所有仓库
            all_repos = []
            page = 1
            while True:
                repos = await self.github_service.get_user_repos(page=page, per_page=100)
                if repos is None:
                    logger.error(f"Issues 定时推送：获取用户 {username} 的仓库列表失败")
                    return
                if not repos:
                    break
                all_repos.extend(repos)
                if len(repos) < 100:
                    break
                page += 1

            if not all_repos:
                logger.info(f"Issues 定时推送：用户 {username} 没有任何仓库")
                return

            # 加载上次快照和推送日志
            old_snapshot = self._load_issues_snapshot()
            push_log = self._load_issues_push_log()

            # 收集当前所有 open issues，构建新快照
            # 快照结构: { "owner/repo": { "issue_number": { "title": ..., "updated_at": ... } } }
            new_snapshot = {}
            for repo in all_repos:
                repo_name = repo["full_name"]
                if repo.get("open_issues_count", 0) == 0:
                    continue

                issues = await self.github_service.get_open_issues(repo["owner"]["login"], repo["name"])
                if not issues:
                    continue

                new_snapshot[repo_name] = {}
                for issue in issues:
                    new_snapshot[repo_name][str(issue["number"])] = {
                        "title": issue["title"],
                        "updated_at": issue["updated_at"],
                        "author": issue["author"],
                        "url": issue["url"],
                        "labels": issue["labels"],
                    }

            # 对比快照，找出新增和更新的 issue
            new_issues = {}  # 之前不存在的 issue
            updated_issues = {}  # 之前存在但 updated_at 变化的 issue
            for repo_name, issues in new_snapshot.items():
                old_repo = old_snapshot.get(repo_name, {})
                for issue_num, issue_data in issues.items():
                    if issue_num not in old_repo:
                        # 新增 issue
                        if repo_name not in new_issues:
                            new_issues[repo_name] = []
                        new_issues[repo_name].append({
                            "number": int(issue_num),
                            "tag": "NEW",
                            **issue_data,
                        })
                    elif issue_data["updated_at"] != old_repo[issue_num].get("updated_at", ""):
                        # 更新的 issue
                        if repo_name not in updated_issues:
                            updated_issues[repo_name] = []
                        updated_issues[repo_name].append({
                            "number": int(issue_num),
                            "tag": "UPDATED",
                            **issue_data,
                        })

            # 保存新快照
            self._save_issues_snapshot(new_snapshot)

            # 如果没有变化，跳过推送
            if not new_issues and not updated_issues:
                logger.info("Issues 定时推送：与上次快照相比无变化，跳过推送")
                return

            # 间隔保护：生成内容指纹，检查是否在短时间内已推送过相同内容
            content_fingerprint = self._build_issues_fingerprint(new_issues, updated_issues)
            last_push_time = push_log.get(content_fingerprint, {}).get("time", "")
            min_interval_minutes = self.config.get("issues_push_min_interval", 60)

            if last_push_time:
                try:
                    last_dt = datetime.fromisoformat(last_push_time)
                    elapsed = (datetime.now(ZoneInfo("UTC")) - last_dt).total_seconds() / 60
                    if elapsed < min_interval_minutes:
                        logger.info(
                            f"Issues 定时推送：距上次推送仅 {elapsed:.0f} 分钟，"
                            f"小于最小间隔 {min_interval_minutes} 分钟，跳过"
                        )
                        return
                except Exception:
                    pass

            # 构建推送消息
            message = "\U0001f4cb " + username + " 的 Issues 变更推送\n\n"
            total_new = 0
            total_updated = 0

            if new_issues:
                message += "\U0001f195 新增 Issues:\n\n"
                for repo_name, issues in new_issues.items():
                    message += "\U0001f4c1 " + repo_name + "\n"
                    for issue in issues:
                        labels_str = ""
                        if issue.get("labels"):
                            labels_str = " \U0001f3f7\ufe0f " + ",".join(issue["labels"])
                        message += "  #" + str(issue["number"]) + " " + issue["title"] + labels_str + "\n"
                        message += "     \U0001f464 " + issue["author"] + " | \U0001f517 " + issue["url"] + "\n"
                        total_new += 1
                    message += "\n"

            if updated_issues:
                message += "\U0001f504 更新 Issues:\n\n"
                for repo_name, issues in updated_issues.items():
                    message += "\U0001f4c1 " + repo_name + "\n"
                    for issue in issues:
                        labels_str = ""
                        if issue.get("labels"):
                            labels_str = " \U0001f3f7\ufe0f " + ",".join(issue["labels"])
                        message += "  #" + str(issue["number"]) + " " + issue["title"] + labels_str + "\n"
                        message += "     \U0001f464 " + issue["author"] + " | \U0001f517 " + issue["url"] + "\n"
                        total_updated += 1
                    message += "\n"

            message += "\U0001f4ca 新增 " + str(total_new) + " 个，更新 " + str(total_updated) + " 个"

            # 私聊推送
            for target in private_targets:
                try:
                    result = await self.notification_service._send_private_message(int(target), message)
                    if result.get("success", False):
                        logger.info(f"Issues 定时推送：私聊成功发送给 {target}")
                    else:
                        logger.warning(f"Issues 定时推送：私聊发送给 {target} 失败")
                except Exception as e:
                    logger.error(f"Issues 定时推送：私聊发送给 {target} 出错: {str(e)}")

            # 群聊推送
            for group_id in group_targets:
                try:
                    result = await self.notification_service._send_group_message(int(group_id), message)
                    if result.get("success", False):
                        logger.info(f"Issues 定时推送：群消息成功发送给 {group_id}")
                    else:
                        logger.warning(f"Issues 定时推送：群消息发送给 {group_id} 失败")
                except Exception as e:
                    logger.error(f"Issues 定时推送：群消息发送给 {group_id} 出错: {str(e)}")

            # 更新推送日志
            push_log[content_fingerprint] = {
                "time": datetime.now(ZoneInfo("UTC")).isoformat(),
                "new_count": total_new,
                "updated_count": total_updated,
            }
            self._save_issues_push_log(push_log)

        except Exception as e:
            logger.error(f"Issues 定时推送失败: {str(e)}")

    def _build_issues_fingerprint(self, new_issues: Dict, updated_issues: Dict) -> str:
        """根据新增和更新的 issue 生成内容指纹，用于间隔保护去重"""
        parts = []
        for repo_name, issues in sorted(new_issues.items()):
            for issue in sorted(issues, key=lambda x: x["number"]):
                parts.append(f"N:{repo_name}#{issue['number']}")
        for repo_name, issues in sorted(updated_issues.items()):
            for issue in sorted(issues, key=lambda x: x["number"]):
                parts.append(f"U:{repo_name}#{issue['number']}")
        return "|".join(parts)

    async def terminate(self):
        # 取消 commit 监控任务
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

        # 取消 issues 定时推送任务
        if self._issues_cron_task and not self._issues_cron_task.done():
            self._issues_cron_task.cancel()
            try:
                await self._issues_cron_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"终止 Issues 定时推送任务时出错: {str(e)}")
        self._issues_cron_task = None

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
