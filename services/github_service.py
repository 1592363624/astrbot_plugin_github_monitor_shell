from typing import Dict, Optional

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
                    branch = "main"  # fallback to main

            url = f"{self.base_url}/repos/{owner}/{repo}/commits/{branch}"

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
            else:
                logger.error(f"获取commit失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    async def get_repository_info(self, owner: str, repo: str) -> Optional[Dict]:
        """获取仓库信息"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}"

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"获取仓库信息失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"获取仓库信息失败: {str(e)}")
            return None
