from typing import Dict, Optional, List

import httpx

from astrbot.api import logger


class GitHubService:
    def __init__(self, token: str = ""):
        self.token = token
        self.base_url = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-GitHub-Monitor"
        }
        if token:
            self.headers["Authorization"] = f"token {token}"

    async def get_latest_commit(self, owner: str, repo: str, branch: str | None = None) -> Optional[Dict]:
        """获取指定仓库最新commit信息"""
        try:
            # 如果没有指定分支，则获取默认分支
            if not branch:
                repo_info = await self.get_repository_info(owner, repo)
                if repo_info and "default_branch" in repo_info:
                    branch = repo_info["default_branch"]
                else:
                    logger.error("无法获取默认分支信息")
                    logger.error(f"请检查仓库 {owner}/{repo} 是否存在，或是否有访问权限。")
                    logger.error(f"并确保可以正常访问 GitHub API：https://api.github.com/repos/{owner}/{repo}")
                    return None

            url = f"{self.base_url}/repos/{owner}/{repo}/commits/{branch}"

            logger.info(f"正在获取最新commit信息: {url}")

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                commit_data = response.json()
                return {
                    "sha": commit_data["sha"],
                    "message": commit_data["commit"]["message"],
                    "author": commit_data["commit"]["author"]["name"],
                    "date": commit_data["commit"]["author"]["date"],
                    "url": commit_data["html_url"],
                    # 单提交详情接口自带变更统计，无需额外请求
                    "stats": self._extract_commit_stats(commit_data)
                }
            elif response.status_code == 404:
                # 仓库不存在或分支不存在
                logger.warning(f"仓库或分支不存在: {owner}/{repo}/{branch}")
                return None
            elif response.status_code == 403:
                # API限制或其他权限问题
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取commit失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    async def get_commits_since(self, owner: str, repo: str, since_sha: str, branch: str | None = None) -> Optional[
        List[Dict]]:
        """获取从指定commit之后的所有commit信息"""
        try:
            # 如果没有指定分支，则获取默认分支
            if not branch:
                repo_info = await self.get_repository_info(owner, repo)
                if repo_info and "default_branch" in repo_info:
                    branch = repo_info["default_branch"]
                else:
                    logger.error("无法获取默认分支信息")
                    logger.error(f"请检查仓库 {owner}/{repo} 是否存在，或是否有访问权限。")
                    logger.error(f"并确保可以正常访问 GitHub API：https://api.github.com/repos/{owner}/{repo}")
                    return None

            # 获取提交列表，从最新的开始，直到since_sha
            url = f"{self.base_url}/repos/{owner}/{repo}/commits"
            params = {
                "sha": branch,
                "per_page": 10  # 限制最多获取10个提交，防止过多数据
            }

            logger.info(f"正在获取commit历史信息: {url}")

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                commits_data = response.json()
                commits = []

                # 收集从最新到since_sha之间的所有提交（不包含since_sha本身）
                for commit_data in commits_data:
                    # 如果到达了上次记录的commit，则停止（不包含这个commit）
                    if commit_data["sha"] == since_sha:
                        break
                        
                    commit = {
                        "sha": commit_data["sha"],
                        "message": commit_data["commit"]["message"],
                        "author": commit_data["commit"]["author"]["name"],
                        "date": commit_data["commit"]["author"]["date"],
                        "url": commit_data["html_url"]
                    }
                    commits.append(commit)

                return commits
            elif response.status_code == 404:
                # 仓库不存在或分支不存在
                logger.warning(f"仓库或分支不存在: {owner}/{repo}/{branch}")
                return None
            elif response.status_code == 403:
                # API限制或其他权限问题
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取commit历史失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    @staticmethod
    def _extract_commit_stats(commit_data: Dict) -> Optional[Dict]:
        """从单提交详情响应中提取变更统计

        合并提交、超大 diff 等场景 GitHub 可能不返回 stats/files，此时返回 None，
        由调用方跳过展示（不阻塞通知）。
        """
        stats = commit_data.get("stats")
        if not isinstance(stats, dict):
            return None
        files = commit_data.get("files")
        return {
            "additions": int(stats.get("additions") or 0),
            "deletions": int(stats.get("deletions") or 0),
            "total": int(stats.get("total") or 0),
            "changed_files": len(files) if isinstance(files, list) else None
        }

    async def get_commit_stats(self, owner: str, repo: str, sha: str) -> Optional[Dict]:
        """获取单个 commit 的变更统计（改动文件数、新增/删除行数）

        注意：commit 列表接口不返回统计信息，只能逐个查询单提交详情接口。
        """
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/commits/{sha}"

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return self._extract_commit_stats(response.json())
            elif response.status_code == 404:
                logger.warning(f"commit 不存在: {owner}/{repo}@{sha[:7]}")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取 commit 统计失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    async def enrich_commit_stats(self, owner: str, repo: str, commits: List[Dict],
                                  max_commits: int = 10) -> List[Dict]:
        """为缺少统计信息的 commit 补全变更统计（原地写入 stats 字段）

        已带 stats 的提交不会重复请求；最多补全 ``max_commits`` 个，
        ``max_commits <= 0`` 时不做任何请求。单个提交查询失败不影响其他提交。
        """
        if max_commits <= 0:
            return commits

        queried = 0
        for commit in commits:
            if queried >= max_commits:
                break
            if commit.get("stats"):
                continue
            sha = commit.get("sha")
            if not sha:
                continue
            queried += 1
            stats = await self.get_commit_stats(owner, repo, sha)
            if stats:
                commit["stats"] = stats
        return commits

    async def get_repository_info(self, owner: str, repo: str) -> Optional[Dict]:
        """获取仓库信息"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}"

            logger.info(f"正在获取仓库信息: {url}")

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                # 仓库不存在
                logger.warning(f"仓库不存在: {owner}/{repo}")
                return None
            elif response.status_code == 403:
                # API限制或其他权限问题
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取仓库信息失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"获取仓库信息失败: {str(e)}")
            return None

    async def get_open_issues(self, owner: str, repo: str) -> Optional[List[Dict]]:
        """获取指定仓库的所有 open issues"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/issues"
            params = {
                "state": "open",
                "per_page": 100  # 每页最多100条
            }

            logger.info(f"正在获取 open issues: {url}")

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                issues_data = response.json()
                issues = []
                for issue in issues_data:
                    # 过滤掉 pull requests（GitHub API 会将 PR 也返回）
                    if "pull_request" in issue:
                        continue
                    issues.append({
                        "number": issue["number"],
                        "title": issue["title"],
                        "author": issue["user"]["login"],
                        "created_at": issue["created_at"],
                        "updated_at": issue["updated_at"],
                        "url": issue["html_url"],
                        "labels": [label["name"] for label in issue.get("labels", [])],
                        "comments": issue.get("comments", 0)
                    })
                return issues
            elif response.status_code == 404:
                logger.warning(f"仓库不存在或无权限: {owner}/{repo}")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取 open issues 失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_issue_comments(self, owner: str, repo: str, issue_number: int) -> Optional[List[Dict]]:
        """获取指定 issue 的最新评论（按时间倒序，最多返回前30条）"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
            params = {
                "sort": "created",
                "direction": "desc",
                "per_page": 30
            }

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                comments_data = response.json()
                return [
                    {
                        "author": c["user"]["login"],
                        "body": (c.get("body") or "").strip(),
                        "created_at": c["created_at"],
                        "url": c["html_url"]
                    }
                    for c in comments_data
                ]
            elif response.status_code == 404:
                logger.warning(f"Issue 或仓库不存在: {owner}/{repo}#{issue_number}")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取 issue 评论失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_current_user(self) -> Optional[Dict]:
        """获取当前认证用户的信息（需要 token）"""
        try:
            url = f"{self.base_url}/user"

            logger.info(f"正在获取当前用户信息: {url}")

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                logger.error("认证失败：Token 无效或未配置")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取用户信息失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_user_repos(self, page: int = 1, per_page: int = 100) -> Optional[List[Dict]]:
        """获取当前认证用户的所有仓库（含私有仓库，支持分页）

        使用 /user/repos 认证接口，而非 /users/{username}/repos，
        后者只返回公开仓库，前者能返回包括私有仓库在内的全部仓库。
        """
        try:
            url = f"{self.base_url}/user/repos"
            params = {
                "type": "owner",  # 只获取用户拥有的仓库，不包含 fork 的
                "sort": "updated",
                "per_page": per_page,
                "page": page
            }

            logger.info(f"正在获取当前用户的所有仓库: {url}")

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                logger.error("认证失败：Token 无效或未配置")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取用户仓库失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None