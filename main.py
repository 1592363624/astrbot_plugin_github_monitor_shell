import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star import StarTools
from .services.github_service import GitHubService
from .services.group_upload_service import GroupUploadService
from .services.image_render_service import ImageRenderService
from .services.notification_service import NotificationService, format_commit_datetime
from .services.onebot_direct import OneBotDirectSender
from .services.template_manager import TemplateManager
from .utils.cron_utils import cron_matches, get_next_run_time
from .utils.markdown_utils import extract_image_urls

# 单条 Issues 动态通知最多补发的图片数量，防止刷屏
MAX_ISSUE_IMAGES_PER_PUSH = 6


class GitHubMonitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.github_service = GitHubService(self.config.get("github_token", ""))
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        # 文转图相关服务（模板目录：插件内置 templates/ + 数据目录自定义 templates/）
        template_manager = TemplateManager(
            builtin_dir=os.path.join(os.path.dirname(__file__), "templates"),
            custom_dir=os.path.join(plugin_data_dir, "templates"),
        )
        image_service = ImageRenderService(
            template_manager, self.html_render, self.config
        )
        upload_service = GroupUploadService(context, self.config)
        image_cfg = self.config.get("image_output", {}) or {}
        onebot_sender = OneBotDirectSender(
            context, enable_base64=bool(image_cfg.get("enable_base64_image", True))
        )
        self.notification_service = NotificationService(
            context, self.config, image_service=image_service,
            upload_service=upload_service, onebot_sender=onebot_sender,
        )
        self.data_file = os.path.join(plugin_data_dir, "commits.json")
        self.sent_notifications_file = os.path.join(plugin_data_dir, "sent_notifications.json")
        self.issues_snapshot_file = os.path.join(plugin_data_dir, "issues_snapshot.json")
        self.issues_push_log_file = os.path.join(plugin_data_dir, "issues_push_log.json")
        self.repo_issues_state_file = os.path.join(plugin_data_dir, "repo_issues_state.json")
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

    def _load_repo_issues_state(self) -> Dict:
        """加载项目仓库 Issues 动态监控状态

        结构: { "owner/repo": { "issue_number": {
            "title": ..., "author": ..., "url": ...,
            "labels": [...], "updated_at": ..., "comments": n, "last_comment_at": ...
        } } }
        """
        try:
            if os.path.exists(self.repo_issues_state_file):
                with open(self.repo_issues_state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载项目仓库 Issues 状态失败: {str(e)}")
            return {}

    def _save_repo_issues_state(self, data: Dict):
        """保存项目仓库 Issues 动态监控状态"""
        try:
            with open(self.repo_issues_state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存项目仓库 Issues 状态失败: {str(e)}")

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

            # 私聊推送（目标支持纯数字QQ号或 UMO 格式：平台ID:FriendMessage:会话ID）
            for target in private_targets:
                try:
                    result = await self.notification_service._send_private_message(str(target), message)
                    if result.get("success", False):
                        logger.info(f"Issues 定时推送：私聊成功发送给 {target}")
                    else:
                        logger.warning(f"Issues 定时推送：私聊发送给 {target} 失败")
                except Exception as e:
                    logger.error(f"Issues 定时推送：私聊发送给 {target} 出错: {str(e)}")

            # 群聊推送（目标支持纯数字群号或 UMO 格式：平台ID:GroupMessage:会话ID）
            for group_id in group_targets:
                try:
                    result = await self.notification_service._send_group_message(str(group_id), message)
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
        updated_count = 0
        
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

            # Issues 动态监控：检测该项目仓库的新 Issue 及 Issue 下的新讨论
            if self.config.get("issue_monitor_enabled", True):
                try:
                    await self._check_repo_issues(owner, repo, extra_groups)
                except Exception as e:
                    logger.error(f"检查仓库 {owner}/{repo} 的 Issues 失败: {str(e)}")

            # 获取最新commit
            new_commit = await self.github_service.get_latest_commit(owner, repo, branch)
            if not new_commit:
                continue

            old_commit = commit_data.get(repo_key)

            # 检查是否有变化
            if not old_commit or old_commit.get("sha") != new_commit["sha"]:
                updated_count += 1
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
                            repo_info, new_commits, notification_targets, all_groups,
                            branch=actual_branch,
                        )
                        # 标记为已发送
                        self._mark_commit_as_notified(repo_key, latest_sha, all_groups)
                        logger.info(f"已标记仓库 {repo_key} 的提交 {latest_sha[:7]} 为已通知")

                # 更新数据
                commit_data[repo_key] = new_commit  # 仍然只保存最新的提交SHA用于比较
                self._save_commit_data(commit_data)
            else:
                logger.info(f"仓库 {repo_key} 无新commit（最新 {new_commit['sha'][:7]}）")

        # 清理已删除仓库的数据
        removed_keys = set(commit_data.keys()) - configured_repo_keys
        for removed_key in removed_keys:
            del commit_data[removed_key]
            logger.info(f"已清理已删除仓库的数据: {removed_key}")
        if removed_keys:
            self._save_commit_data(commit_data)

        logger.info(
            f"本轮检查完成：共 {len(configured_repo_keys)} 个仓库，{updated_count} 个有更新"
        )

    async def _check_repo_issues(self, owner: str, repo: str, extra_groups: List[str] = None):
        """检测单个项目仓库的 Issues 动态

        - 有新增 Issue 时推送通知
        - 已有 Issue 出现新讨论变化（新增评论、标题/正文/标签更新等）时推送通知，
          新增评论会附带评论者与内容摘要
        - 首次监控某仓库时仅建立基线快照，不推送，避免刷屏
        """
        if extra_groups is None:
            extra_groups = []

        repo_name = f"{owner}/{repo}"
        issues = await self.github_service.get_open_issues(owner, repo)
        if issues is None:
            logger.warning(f"Issues 动态监控：获取 {repo_name} 的 issues 失败，本轮跳过")
            return

        state = self._load_repo_issues_state()
        old_repo_state = state.get(repo_name)
        first_run = old_repo_state is None  # 该仓库首次纳入监控，只建基线不推送

        new_snapshot = {}
        new_issues = []       # 新增的 issue
        updated_issues = []   # 有动态的 issue，元素: {"issue": {...}, "new_comments": [...]}

        for issue in issues:
            num = str(issue["number"])
            entry = {
                "title": issue["title"],
                "author": issue["author"],
                "url": issue["url"],
                "labels": issue["labels"],
                "updated_at": issue["updated_at"],
                "comments": issue.get("comments", 0),
            }
            new_snapshot[num] = entry

            if first_run:
                continue

            old = old_repo_state.get(num)
            if old is None:
                new_issues.append({**entry, "number": issue["number"]})
                continue

            comment_added = issue.get("comments", 0) > old.get("comments", 0)
            content_changed = issue["updated_at"] != old.get("updated_at", "")
            if not comment_added and not content_changed:
                continue

            # 有新评论时拉取评论详情，筛选出上次记录之后的新评论
            new_comments = []
            if comment_added:
                comments = await self.github_service.get_issue_comments(owner, repo, issue["number"])
                if comments:
                    last_seen = old.get("last_comment_at", "")
                    new_comments = [
                        c for c in comments
                        if not last_seen or c["created_at"] > last_seen
                    ]
                    latest_time = max(
                        [c["created_at"] for c in new_comments] +
                        ([last_seen] if last_seen else []) +
                        [comments[0]["created_at"]]
                    )
                    # entry 已被 new_snapshot 引用，这里更新会同步写入快照
                    entry["last_comment_at"] = latest_time

            updated_issues.append({
                "issue": {**entry, "number": issue["number"]},
                "new_comments": new_comments,
            })

        # 更新快照（不在 open 列表中的 issue 视为已关闭，自动移除）
        state[repo_name] = new_snapshot
        self._save_repo_issues_state(state)

        if first_run:
            logger.info(f"Issues 动态监控：已为 {repo_name} 建立基线快照（{len(new_snapshot)} 个 open issues），本次不推送")
            return

        if not new_issues and not updated_issues:
            logger.info(f"Issues 动态监控：{repo_name} 无变化")
            return

        message = f"\U0001f4cb Issues 动态 - {repo_name}\n\n"
        total_new = len(new_issues)
        total_updated = len(updated_issues)

        # 收集评论正文中的图片链接，文本消息发出后以图片消息补发
        issue_images: List[str] = []

        for issue in new_issues:
            labels_str = ""
            if issue.get("labels"):
                labels_str = " \U0001f3f7\ufe0f " + ",".join(issue["labels"])
            message += f"\U0001f195 新增 Issue #{issue['number']} {issue['title']}{labels_str}\n"
            message += f"   \U0001f464 {issue['author']} | \U0001f517 {issue['url']}\n\n"

        for item in updated_issues:
            issue = item["issue"]
            new_comments = item["new_comments"]
            if new_comments:
                message += (
                    f"\U0001f4ac Issue #{issue['number']} {issue['title']} "
                    f"有新讨论（+{len(new_comments)} 条评论）\n"
                )
                for c in new_comments[:3]:
                    body_text, image_urls = extract_image_urls(c.get("body", ""))
                    c["body"] = body_text
                    for u in image_urls:
                        if u not in issue_images and len(issue_images) < MAX_ISSUE_IMAGES_PER_PUSH:
                            issue_images.append(u)
                    message += f"   └ {c['author']}:\n{body_text}\n\n"
                if len(new_comments) > 3:
                    message += f"   └ ...另有 {len(new_comments) - 3} 条新评论\n"
            else:
                message += f"\U0001f504 Issue #{issue['number']} {issue['title']} 有更新\n"
            message += f"   \U0001f517 {issue['url']}\n\n"

        message += f"\U0001f4ca 新增 {total_new} 个，{total_updated} 个有新动态"

        private_targets = self.config.get("notification_targets", [])
        global_groups = self.config.get("group_notification_targets", [])
        all_groups = list(set(global_groups + extra_groups))

        # 评论中带图片时，把文本与图片合并为同一条消息链（文字在前，图片紧随其后），
        # Image.fromURL 经平台发送链自动下载转发
        payload = message
        if issue_images:
            try:
                payload = MessageChain(
                    chain=[Comp.Plain(message)]
                    + [Comp.Image.fromURL(u) for u in issue_images]
                )
            except Exception as e:
                logger.warning(f"Issues 动态通知：构造图文消息链失败，仅发送文本: {str(e)}")
                payload = message

        for target in private_targets:
            try:
                result = await self.notification_service._send_private_message(str(target), payload)
                if result.get("success", False):
                    logger.info(f"Issues 动态通知：私聊成功发送给 {target}")
                else:
                    logger.warning(f"Issues 动态通知：私聊发送给 {target} 失败")
            except Exception as e:
                logger.error(f"Issues 动态通知：私聊发送给 {target} 出错: {str(e)}")

        for group_id in all_groups:
            try:
                result = await self.notification_service._send_group_message(str(group_id), payload)
                if result.get("success", False):
                    logger.info(f"Issues 动态通知：群消息成功发送给 {group_id}")
                else:
                    logger.warning(f"Issues 动态通知：群消息发送给 {group_id} 失败")
            except Exception as e:
                logger.error(f"Issues 动态通知：群消息发送给 {group_id} 出错: {str(e)}")

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
