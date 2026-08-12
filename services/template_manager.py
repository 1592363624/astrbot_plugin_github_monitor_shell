"""commit 卡片模板管理：内置主题 + 数据目录自定义模板。

本模块不依赖 astrbot，可独立测试。

模板解析优先级：
1. 自定义目录 ``<数据目录>/templates/<名称>.html``
2. 内置主题 ``<插件目录>/templates/<名称>/commit_card.html``
3. 回退到 DEFAULT_THEME
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("github_monitor.templates")

DEFAULT_THEME = "terminal"
BUILTIN_TEMPLATE_FILE = "commit_card.html"


class TemplateManager:
    def __init__(self, builtin_dir: str, custom_dir: Optional[str] = None):
        self.builtin_dir = builtin_dir
        self.custom_dir = custom_dir
        self._env_cache: Dict[str, object] = {}

    def list_templates(self) -> List[str]:
        """列出所有可用模板名（自定义优先，重名时自定义覆盖内置）"""
        names: List[str] = []
        if self.custom_dir and os.path.isdir(self.custom_dir):
            for fname in sorted(os.listdir(self.custom_dir)):
                if fname.endswith(".html"):
                    names.append(fname[: -len(".html")])
        if os.path.isdir(self.builtin_dir):
            for entry in sorted(os.listdir(self.builtin_dir)):
                if entry in names:
                    continue
                if os.path.isfile(
                    os.path.join(self.builtin_dir, entry, BUILTIN_TEMPLATE_FILE)
                ):
                    names.append(entry)
        return names

    def _resolve(self, name: str) -> Optional[Tuple[str, str]]:
        """解析模板名，返回 (模板目录, 模板文件名)，未找到返回 None"""
        # 防路径穿越，只取文件名部分
        safe_name = os.path.basename(str(name)).strip()
        if not safe_name:
            return None

        if self.custom_dir:
            custom_file = safe_name + ".html"
            if os.path.isfile(os.path.join(self.custom_dir, custom_file)):
                return self.custom_dir, custom_file

        theme_dir = os.path.join(self.builtin_dir, safe_name)
        if os.path.isfile(os.path.join(theme_dir, BUILTIN_TEMPLATE_FILE)):
            return theme_dir, BUILTIN_TEMPLATE_FILE

        return None

    def render(self, template_name: str, data: Dict) -> str:
        """渲染指定模板，找不到时回退到默认主题"""
        resolved = self._resolve(template_name)
        if resolved is None and template_name != DEFAULT_THEME:
            logger.warning(
                f"模板 '{template_name}' 不存在，回退到默认主题 '{DEFAULT_THEME}'"
            )
            resolved = self._resolve(DEFAULT_THEME)
        if resolved is None:
            raise FileNotFoundError(
                f"模板 '{template_name}' 和默认主题 '{DEFAULT_THEME}' 均不存在"
            )

        template_dir, template_file = resolved
        env = self._get_env(template_dir)
        return env.get_template(template_file).render(**data)

    def _get_env(self, template_dir: str):
        if template_dir not in self._env_cache:
            try:
                from jinja2 import Environment, FileSystemLoader, select_autoescape
            except ImportError as e:
                raise RuntimeError(
                    "缺少 jinja2 依赖，无法使用文转图功能，请安装 jinja2 或改用文本模式"
                ) from e
            self._env_cache[template_dir] = Environment(
                loader=FileSystemLoader(template_dir),
                autoescape=select_autoescape(["html"]),
                trim_blocks=True,
                lstrip_blocks=True,
            )
        return self._env_cache[template_dir]
