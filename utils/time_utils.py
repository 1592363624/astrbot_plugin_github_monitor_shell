from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


def format_commit_datetime(
    date_str: str,
    time_zone: str,
    time_format: str,
) -> Optional[str]:
    try:
        normalized = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        target_tz = ZoneInfo(time_zone)
        return dt.astimezone(target_tz).strftime(time_format)
    except Exception:
        return None
