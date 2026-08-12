"""群文件/群相册上传服务（OneBot / NapCat 扩展 API）。

仅在图片通知成功发送到 QQ 群后触发；任何一步失败只记日志，不影响消息发送。
API 调用方式参考 astrbot_plugin_qq_group_daily_analysis 的 onebot_adapter。
"""

from typing import Dict, Optional

from astrbot.api import logger

from .onebot_direct import get_onebot_client


class GroupUploadService:
    def __init__(self, context, config: Dict | None = None):
        self.context = context
        image_cfg = (config or {}).get("image_output", {}) or {}
        self.enable_file_upload = bool(image_cfg.get("enable_group_file_upload", False))
        self.file_folder = (image_cfg.get("group_file_folder") or "").strip()
        self.enable_album_upload = bool(image_cfg.get("enable_group_album_upload", False))
        self.album_name = (image_cfg.get("group_album_name") or "").strip()
        self.album_strict_mode = bool(image_cfg.get("group_album_strict_mode", True))

    def _get_onebot_client(self):
        """查找 aiocqhttp 平台实例并返回其 OneBot 客户端"""
        return get_onebot_client(self.context)

    async def maybe_upload(self, group_id, image_b64: str, filename: str) -> None:
        """按配置把图片备份上传到群文件/群相册（仅数字 QQ 群）"""
        if not (self.enable_file_upload or self.enable_album_upload):
            return

        group_id_str = str(group_id)
        if not group_id_str.isdigit():
            return

        client = self._get_onebot_client()
        if client is None or not hasattr(client, "call_action"):
            logger.debug("群文件/相册上传：未找到 aiocqhttp 平台客户端，跳过")
            return

        gid = int(group_id_str)
        if self.enable_file_upload:
            await self._upload_group_file(client, gid, image_b64, filename)
        if self.enable_album_upload:
            await self._upload_group_album(client, gid, image_b64)

    async def _upload_group_file(
        self, client, group_id: int, image_b64: str, filename: str
    ) -> None:
        try:
            folder_id = await self._find_or_create_folder(client, group_id)
            params = {
                "group_id": group_id,
                "file": f"base64://{image_b64}",
                "name": filename,
            }
            if folder_id:
                params["folder"] = folder_id
            await client.call_action("upload_group_file", **params)
            logger.info(f"已上传 commit 卡片到群 {group_id} 的群文件: {filename}")
        except Exception as e:
            logger.error(f"上传群文件失败 (群 {group_id}): {e}")

    async def _find_or_create_folder(self, client, group_id: int) -> Optional[str]:
        """按配置名查找群文件夹，不存在则创建；未配置文件夹名则返回 None（根目录）"""
        if not self.file_folder:
            return None

        folder_id = await self._find_folder_id(client, group_id)
        if folder_id:
            return folder_id

        try:
            await client.call_action(
                "create_group_file_folder", group_id=group_id, name=self.file_folder
            )
        except Exception as e:
            logger.warning(f"创建群文件夹 '{self.file_folder}' 失败: {e}")
        # 创建后重新查询确认 folder_id
        return await self._find_folder_id(client, group_id)

    async def _find_folder_id(self, client, group_id: int) -> Optional[str]:
        try:
            resp = await client.call_action("get_group_root_files", group_id=group_id)
            folders = (resp or {}).get("folders") or []
            for folder in folders:
                if folder.get("folder_name") == self.file_folder:
                    return folder.get("folder_id")
        except Exception as e:
            logger.warning(f"获取群 {group_id} 根目录文件列表失败: {e}")
        return None

    async def _upload_group_album(self, client, group_id: int, image_b64: str) -> None:
        """上传到群相册（NapCat 扩展 API，其他 OneBot 实现可能不支持）"""
        try:
            album_id = await self._find_album_id(client, group_id)
            if album_id is None:
                return

            params = {
                "group_id": group_id,
                "file": f"base64://{image_b64}",
                "album_id": str(album_id),
            }
            if self.album_name:
                params["album_name"] = self.album_name

            for action in (
                "upload_image_to_qun_album",
                "upload_group_album",
                "upload_qun_album",
            ):
                try:
                    await client.call_action(action, **params)
                    logger.info(
                        f"已上传 commit 卡片到群 {group_id} 的群相册 (接口: {action})"
                    )
                    return
                except Exception:
                    continue
            logger.warning(f"群 {group_id} 所有相册上传接口均调用失败")
        except Exception as e:
            logger.error(f"上传群相册失败 (群 {group_id}): {e}")

    async def _find_album_id(self, client, group_id: int) -> Optional[str]:
        albums = []
        for action in (
            "get_qun_album_list",
            "get_group_album_list",
            "get_group_albums",
            "get_group_root_album_list",
        ):
            try:
                resp = await client.call_action(action, group_id=group_id)
                albums = self._extract_album_list(resp)
                if albums:
                    break
            except Exception:
                continue

        if not albums:
            logger.warning(f"群 {group_id} 未获取到相册列表，跳过相册上传")
            return None

        if self.album_name:
            for album in albums:
                name = album.get("name") or album.get("album_name")
                if name == self.album_name:
                    return album.get("album_id")
            if self.album_strict_mode:
                logger.warning(
                    f"群 {group_id} 未找到名为 '{self.album_name}' 的相册，"
                    "严格模式下放弃上传（防止误传到默认相册）"
                )
                return None

        # 非严格模式或未配置相册名：使用列表第一个相册
        fallback = albums[0].get("album_id")
        if fallback is None:
            logger.warning(f"群 {group_id} 相册列表中未找到有效的 album_id")
        return fallback

    @staticmethod
    def _extract_album_list(payload) -> list:
        """从不同结构的响应中提取相册列表"""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, dict):
            album_list = data.get("album_list") or data.get("list")
            if isinstance(album_list, list):
                return [item for item in album_list if isinstance(item, dict)]
        album_list = payload.get("album_list") or payload.get("list")
        if isinstance(album_list, list):
            return [item for item in album_list if isinstance(item, dict)]
        return []
