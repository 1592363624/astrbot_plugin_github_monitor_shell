"""OneBot 直发图片（绕过 StarTools 消息链）。

背景：AstrBot v4 的 aiocqhttp 发送链会把 Image 段统一转成 base64:// 再发给
协议端（aiocqhttp_message_event._from_segment_to_dict），经 StarTools 发送时
enable_base64_image 配置实际不生效。本模块直接调用 OneBot API 发送图片，
传输方式由 enable_base64 决定：base64:// 或本地文件路径（file://）。
"""

from typing import Optional

from astrbot.api import logger


def get_onebot_client(context):
    """查找 aiocqhttp 平台实例并返回其 OneBot 客户端，未找到返回 None"""
    try:
        for platform in context.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.name != "aiocqhttp":
                continue
            get_client = getattr(platform, "get_client", None)
            if callable(get_client):
                return get_client()
            return getattr(platform, "bot", None)
    except Exception as e:
        logger.error(f"查找 aiocqhttp 平台客户端失败: {e}")
    return None


class OneBotDirectSender:
    """通过 OneBot API 直接发送图片消息"""

    def __init__(self, context, enable_base64: bool = True):
        self.context = context
        self.enable_base64 = enable_base64

    async def send_image(
        self,
        target_id: str,
        image_b64: str,
        temp_path: Optional[str] = None,
        is_group: bool = True,
    ) -> Optional[bool]:
        """直发图片消息。

        Returns:
            True/False: 发送成功/失败；None 表示非 OneBot 环境（未找到
            aiocqhttp 平台或目标非法），由调用方改用 StarTools 通道。
        """
        if not str(target_id).isdigit():
            return None

        client = get_onebot_client(self.context)
        if client is None or not hasattr(client, "call_action"):
            return None

        if self.enable_base64:
            file_val = f"base64://{image_b64}"
        elif temp_path:
            file_val = (
                f"file://{temp_path}"
                if temp_path.startswith("/")
                else f"file:///{temp_path}"
            )
        else:
            file_val = f"base64://{image_b64}"

        action = "send_group_msg" if is_group else "send_private_msg"
        id_key = "group_id" if is_group else "user_id"
        mode = "base64" if file_val.startswith("base64://") else "file"
        logger.info(f"OneBot 直发图片: 目标 {target_id} ({action}), 模式 {mode}")
        try:
            await client.call_action(
                action,
                **{id_key: int(target_id)},
                message=[{"type": "image", "data": {"file": file_val}}],
            )
            logger.info(f"OneBot 直发图片成功: {target_id}")
            return True
        except Exception as e:
            logger.error(f"OneBot 直发图片失败 (目标 {target_id}): {e}")
            return False
