"""文件工具：JSON 原子写入

直接以 "w" 模式覆盖写入时，进程若在写入中途退出，磁盘上会留下截断的 JSON，
下次读取解析失败只能整份丢弃（提交记录、已发送记录、失败重试队列都是如此）。
这里先写同目录临时文件再 os.replace 原子替换，保证磁盘上要么是旧内容、
要么是完整的新内容。
"""

import json
import os
from typing import Any


def write_json_atomic(path: str, data: Any, indent: int = 2) -> None:
    """原子地把 data 以 JSON 写入 path（临时文件 + os.replace 替换）

    Args:
        path: 目标文件路径，父目录不存在时自动创建。
        data: 任意可 JSON 序列化的数据。
        indent: json.dump 的缩进，默认 2。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        # 写入失败时清掉半成品临时文件，避免残留；原文件保持可用
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise
