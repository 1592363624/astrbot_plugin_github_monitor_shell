"""配置解析工具：兼容 AstrBot 对 list 配置项的序列化差异。

AstrBot 的 list 配置里，对象条目可能被存成 JSON 字符串
（如 '{"owner":"o","repo":"r","branch":"dev"}'），
本模块在插件读取配置时将其统一还原为 dict。
"""

import json
from typing import Any


def parse_repo_config_item(item: Any):
    """把配置中的单个仓库条目统一解析。

    - 已是 dict：原样返回
    - JSON 字符串形式的 dict：还原为 dict
    - 普通字符串（"owner/repo|group1|group2"）：原样返回，由调用方继续解析
    """
    if isinstance(item, str) and item.strip().startswith("{"):
        try:
            parsed = json.loads(item)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return item
