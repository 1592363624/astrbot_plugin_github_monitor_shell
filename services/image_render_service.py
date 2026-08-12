"""commit 通知文转图服务：Jinja2 渲染 HTML + AstrBot T2I 截图。

渲染流程参考 astrbot_plugin_qq_group_daily_analysis：
- 调用框架 ``Star.html_render(html, {}, False, options)``（return_url=False 返回图片数据）
- 两轮回退策略（第一轮按配置，第二轮固定 jpeg/80/high）
- 校验 magic bytes（JPEG FF D8 / PNG 89PNG），防止把 T2I 错误页当图片发出
"""

import base64
import logging
import os
import random
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from ..utils.time_utils import format_commit_datetime

logger = logging.getLogger("github_monitor.image_render")

JPEG_MAGIC = b"\xff\xd8"
JPEG_TRAILER = b"\xff\xd9"
PNG_MAGIC = b"\x89PNG"
PNG_TRAILER = b"IEND\xaeB\x60\x82"


def is_valid_image(data: bytes) -> bool:
    """校验图片完整性：头部 magic bytes + 尾部结束标记（截断图会导致 NapCat 解码失败）"""
    if not data or len(data) < 16:
        return False
    if data.startswith(JPEG_MAGIC):
        return data.endswith(JPEG_TRAILER)
    if data.startswith(PNG_MAGIC):
        return data.endswith(PNG_TRAILER)
    return False


class ImageRenderService:
    """将 commit 更新渲染为图片（返回 base64 字符串）"""

    def __init__(self, template_manager, html_render_func, config: Dict | None = None):
        self.template_manager = template_manager
        self.html_render_func = html_render_func
        config = config or {}
        image_cfg = config.get("image_output", {}) or {}
        self.template_name = image_cfg.get("commit_image_template", "terminal")
        self.t2i_image_type = image_cfg.get("t2i_image_type", "png")
        self.t2i_quality = int(image_cfg.get("t2i_quality", 90) or 90)
        self.t2i_scale = image_cfg.get("t2i_scale", "high")
        self.time_zone = config.get("time_zone", "Asia/Shanghai")
        self.time_format = config.get("time_format", "%Y-%m-%d %H:%M:%S")

    def available(self) -> bool:
        return callable(self.html_render_func)

    def build_render_data(
        self, repo_info: Dict, new_commits: List[Dict], branch: Optional[str] = None
    ) -> Dict:
        """组装模板上下文数据"""
        owner = (repo_info.get("owner") or {}).get("login") or ""
        repo = repo_info.get("name") or ""

        commits = []
        for commit in new_commits:
            formatted = format_commit_datetime(
                commit.get("date", ""), self.time_zone, self.time_format
            )
            commits.append(
                {
                    "sha_short": (commit.get("sha") or "")[:7],
                    "message": commit.get("message", ""),
                    "author": commit.get("author", ""),
                    "time": formatted or commit.get("date", ""),
                    "url": commit.get("url", ""),
                }
            )

        try:
            generated_at = datetime.now(ZoneInfo(self.time_zone)).strftime(
                self.time_format
            )
        except Exception:
            generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return {
            "title": "GitHub 仓库更新通知",
            "repo_name": f"{owner}/{repo}",
            "repo_url": repo_info.get("html_url", ""),
            "branch": branch,
            "commit_count": len(commits),
            "generated_at": generated_at,
            "commits": commits,
        }

    def _resolve_template_name(self) -> str:
        """解析实际使用的模板名；配置为 random 时从所有可用模板中随机选一个"""
        if self.template_name != "random":
            return self.template_name
        candidates = self.template_manager.list_templates()
        if not candidates:
            return self.template_name
        chosen = random.choice(candidates)
        logger.info(f"随机选择 commit 卡片模板: {chosen}")
        return chosen

    def _render_strategies(self) -> List[Dict]:
        """两轮渲染策略：第一轮按配置，第二轮固定回退 jpeg/80/high"""
        first = {
            "type": self.t2i_image_type,
            "device_scale_factor_level": self.t2i_scale,
            "timeout": 50000,
        }
        if self.t2i_image_type == "jpeg":
            first["quality"] = self.t2i_quality
        second = {
            "type": "jpeg",
            "quality": 80,
            "device_scale_factor_level": "high",
            "timeout": 100000,
        }
        return [first, second]

    @staticmethod
    async def _coerce_bytes(result) -> Optional[bytes]:
        """html_render 返回值统一为 bytes（可能是 bytes 或本地临时文件路径）"""
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if isinstance(result, str) and os.path.isfile(result):
            try:
                with open(result, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.remove(result)
                except OSError:
                    pass
        return None

    async def render_commit_image(
        self, repo_info: Dict, new_commits: List[Dict], branch: Optional[str] = None
    ) -> Optional[str]:
        """渲染 commit 卡片，成功返回 base64 字符串，失败返回 None（调用方计入重试队列）"""
        if not self.available():
            logger.error("文转图功能不可用：html_render 未注入")
            return None

        try:
            data = self.build_render_data(repo_info, new_commits, branch)
            template_name = self._resolve_template_name()
            html = self.template_manager.render(template_name, data)
        except Exception as e:
            logger.error(f"渲染 commit 卡片模板失败: {e}")
            return None

        logger.info(
            f"开始 T2I 渲染 commit 卡片: {data['repo_name']}, "
            f"模板 {template_name}, {data['commit_count']} 个提交"
        )

        for attempt, options in enumerate(self._render_strategies(), 1):
            try:
                result = await self.html_render_func(html, {}, False, options)
            except Exception as e:
                logger.error(f"T2I 第 {attempt} 轮渲染异常: {e}")
                continue

            image_bytes = await self._coerce_bytes(result)
            if image_bytes and is_valid_image(image_bytes):
                logger.info(
                    f"commit 卡片渲染成功: 模板 {template_name}, "
                    f"{len(image_bytes) / 1024:.1f} KB"
                )
                return base64.b64encode(image_bytes).decode()
            size = len(image_bytes) if image_bytes else 0
            logger.warning(f"T2I 第 {attempt} 轮返回了无效的图片数据 ({size} 字节)")

        logger.error("T2I 所有渲染轮次均失败")
        return None
