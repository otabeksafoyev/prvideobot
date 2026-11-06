from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

from config import Settings
from downloader import UserFacingError

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]


class TelegramUploader:
    """Katta fayllarni (2 GB gacha) MTProto orqali bot nomidan yuboradi."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)

    async def start(self) -> None:
        await self.client.start(bot_token=self.settings.bot_token)

        # Foydalanuvchi xabarlarini qabul qilish — Telethon foydalanuvchini
        # shu orqali taniydi (entity keshiga tushadi).
        @self.client.on(events.NewMessage(incoming=True))
        async def _remember(event):  # noqa: ANN001
            return

    async def stop(self) -> None:
        await self.client.disconnect()

    async def _resolve(self, chat_id: int, username: str | None):
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            pass
        if username:
            try:
                return await self.client.get_input_entity(username)
            except Exception:  # noqa: BLE001
                pass
        raise UserFacingError("Qabul qiluvchini aniqlab bo'lmadi. Havolani botga qayta yuboring.")

    async def send_video(
        self,
        chat_id: int,
        username: str | None,
        path: Path,
        caption: str,
        duration: int,
        width: int | None,
        height: int | None,
        filename: str,
        on_progress: ProgressCb,
    ) -> None:
        entity = await self._resolve(chat_id, username)
        await self.client.send_file(
            entity,
            str(path),
            caption=caption,
            parse_mode="html",
            supports_streaming=True,
            attributes=[
                DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=int(width or 0),
                    h=int(height or 0),
                    supports_streaming=True,
                ),
                DocumentAttributeFilename(filename),
            ],
            progress_callback=on_progress,
        )
