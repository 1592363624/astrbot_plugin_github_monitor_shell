"""Issue 评论 Markdown 正文工具。

GitHub 评论里的截图会以 markdown 图片语法写入正文，例如：

    ![Image](https://github.com/user-attachments/assets/xxxx)

QQ 消息里直接发 markdown 原文只会显示一串链接文字，图片本体无法展示。
本模块负责把正文中的图片链接抽取出来（供后续以图片消息补发），并把
原文中的图片语法替换为「[图片]」占位符，避免 QQ 里出现冗长链接。
"""

import re
from typing import List, Tuple

# markdown 图片语法: ![alt](url) 或 ![alt](url "title")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^\s)]+)[^)]*\)")

# HTML 图片标签（GitHub 评论上传截图时自动转为带尺寸属性的 <img src="...">）
_HTML_IMG_RE = re.compile(
    r"<img\b[^>]*?\bsrc=[\"'](https?://[^\"'\s]+)[\"'][^>]*>/?>?",
    re.IGNORECASE,
)

# 裸贴的图片直链（常见图片扩展名结尾，可带查询参数）
_BARE_IMAGE_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]]+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>()\[\]]*)?",
    re.IGNORECASE,
)

_IMAGE_PLACEHOLDER = "[图片]"


def extract_image_urls(text: str, max_images: int = 6) -> Tuple[str, List[str]]:
    """从 markdown 文本中提取图片链接。

    Args:
        text: 原始 markdown 文本。
        max_images: 最多提取的图片数量上限。

    Returns:
        (cleaned_text, image_urls) 元组：
        - cleaned_text: 图片语法被替换为 [图片] 占位符后的文本；
        - image_urls: 按出现顺序去重后的图片 URL 列表。
    """
    text = text or ""
    urls: List[str] = []

    def _collect_md(match: re.Match) -> str:
        url = match.group(1)
        if url not in urls and len(urls) < max_images:
            urls.append(url)
        return _IMAGE_PLACEHOLDER

    cleaned = _MD_IMAGE_RE.sub(_collect_md, text)
    cleaned = _HTML_IMG_RE.sub(_collect_md, cleaned)

    # 未达上限时，额外识别裸贴的图片直链（同样替换为占位符）
    if len(urls) < max_images:
        def _collect_bare(match: re.Match) -> str:
            url = match.group(0)
            if url not in urls and len(urls) < max_images:
                urls.append(url)
                return _IMAGE_PLACEHOLDER
            return match.group(0)

        cleaned = _BARE_IMAGE_URL_RE.sub(_collect_bare, cleaned)

    return cleaned, urls
