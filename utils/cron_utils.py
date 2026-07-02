"""
cron_utils.py - Cron 表达式解析与匹配工具

支持标准 5 段 Cron 表达式（分 时 日 月 周），
用于判断当前时间是否匹配指定的 Cron 表达式。
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional


def parse_cron_field(field: str, min_val: int, max_val: int) -> set:
    """解析单个 Cron 字段，返回匹配的值集合

    Args:
        field: Cron 字段字符串，支持 *、数字、逗号分隔、范围(-)、步长(/)
        min_val: 该字段的最小值
        max_val: 该字段的最大值

    Returns:
        匹配的值集合
    """
    values = set()

    for part in field.split(","):
        if "/" in part:
            # 步长：range/step 或 */step
            range_part, step = part.split("/", 1)
            step = int(step)
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                start, end = map(int, range_part.split("-", 1))
            else:
                start = int(range_part)
                end = max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            # 范围：start-end
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        elif part == "*":
            # 通配符：所有值
            values.update(range(min_val, max_val + 1))
        else:
            # 单个数字
            values.add(int(part))

    return values


def cron_matches(cron_expression: str, dt: datetime, time_zone: str = "Asia/Shanghai") -> bool:
    """判断给定时间是否匹配 Cron 表达式

    Args:
        cron_expression: 标准 5 段 Cron 表达式（分 时 日 月 周）
        dt: 要判断的时间（UTC 时间）
        time_zone: 时区名称（IANA 标准）

    Returns:
        是否匹配
    """
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return False

    # 将 UTC 时间转换为目标时区
    target_tz = ZoneInfo(time_zone)
    local_dt = dt.astimezone(target_tz)

    minute, hour, day, month, weekday = parts

    # 解析各字段（Cron 中周日=0 或 7）
    minute_vals = parse_cron_field(minute, 0, 59)
    hour_vals = parse_cron_field(hour, 0, 23)
    day_vals = parse_cron_field(day, 1, 31)
    month_vals = parse_cron_field(month, 1, 12)
    weekday_vals = parse_cron_field(weekday, 0, 7)

    # Python weekday: Monday=0, Sunday=6; Cron: Sunday=0, Monday=1
    # 转换 Python weekday 到 Cron weekday
    cron_weekday = (local_dt.weekday() + 1) % 7

    return (
        local_dt.minute in minute_vals
        and local_dt.hour in hour_vals
        and local_dt.day in day_vals
        and local_dt.month in month_vals
        and (cron_weekday in weekday_vals or 7 in weekday_vals)
    )


def get_next_run_time(cron_expression: str, time_zone: str = "Asia/Shanghai") -> Optional[datetime]:
    """获取 Cron 表达式的下一次执行时间（粗略估算，用于日志显示）

    Args:
        cron_expression: 标准 5 段 Cron 表达式
        time_zone: 时区名称

    Returns:
        下一次执行时间的字符串描述，如果无法解析则返回 None
    """
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return None

    minute, hour, day, month, weekday = parts

    # 简单描述
    if minute == "*" and hour == "*" and day == "*" and month == "*" and weekday == "*":
        return "每分钟"
    elif minute != "*" and hour == "*" and day == "*" and month == "*" and weekday == "*":
        return f"每小时的第 {minute} 分钟"
    elif minute != "*" and hour != "*" and day == "*" and month == "*" and weekday == "*":
        return f"每天 {hour.zfill(2)}:{minute.zfill(2)}"
    elif minute != "*" and hour != "*" and day != "*" and month == "*" and weekday == "*":
        return f"每月 {day} 日 {hour.zfill(2)}:{minute.zfill(2)}"
    elif minute != "*" and hour != "*" and day == "*" and month == "*" and weekday != "*":
        weekday_names = {
            "0": "周日", "1": "周一", "2": "周二", "3": "周三",
            "4": "周四", "5": "周五", "6": "周六", "7": "周日"
        }
        if "-" in weekday:
            start, end = weekday.split("-", 1)
            return f"每周 {weekday_names.get(start, start)} 到 {weekday_names.get(end, end)} {hour.zfill(2)}:{minute.zfill(2)}"
        elif "," in weekday:
            days = [weekday_names.get(d.strip(), d.strip()) for d in weekday.split(",")]
            return f"每周 {'、'.join(days)} {hour.zfill(2)}:{minute.zfill(2)}"
        else:
            return f"每周 {weekday_names.get(weekday, weekday)} {hour.zfill(2)}:{minute.zfill(2)}"

    return cron_expression
