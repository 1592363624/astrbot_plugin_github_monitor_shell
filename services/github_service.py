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
                    "url": commit_data["html_url"]
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
                        "labels": [label["name"] for label in issue.get("labels", [])]
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