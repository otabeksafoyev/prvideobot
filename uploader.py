from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, events, helpers
from telethon.sessions import StringSession
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, InputFileBig

from config import Settings
from downloader import UserFacingError

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]
PART_SIZE = 512 * 1024  # Telegram ruxsat etgan eng katta bo'lak


class TelegramUploader:
    """Katta fayllarni (2 GB gacha) MTProto orqali bot nomidan yuboradi.
    Bo'laklar parallel yuboriladi (Telethon odatda ketma-ket yuboradi)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)

    async def start(self) -> None:
        await self.client.start(bot_token=self.settings.bot_token)

        # Foydalanuvchi xabarlarini qabul qilish: Telethon foydalanuvchini
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

    async def _parallel_upload(self, path: Path, filename: str, on_progress: ProgressCb) -> InputFileBig:
        size = path.stat().st_size
        parts = (size + PART_SIZE - 1) // PART_SIZE
        file_id = helpers.generate_random_long()
        sem = asyncio.Semaphore(self.settings.upload_workers)
        sent = 0
        errors: list[BaseException] = []
        tasks: list[asyncio.Task] = []

        async def send(index: int, data: bytes) -> None:
            nonlocal sent
            try:
                ok = await self.client(SaveBigFilePartRequest(file_id, index, parts, data))
                if not ok:
                    raise RuntimeError("Telegram bo'lakni qabul qilmadi")
                sent += len(data)
                await on_progress(sent, size)
            finally:
                sem.release()

        def done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                errors.append(task.exception())

        try:
            with open(path, "rb") as f:
                for index in range(parts):
                    await sem.acquire()  # xotira: bir vaqtda faqat N ta bo'lak
                    if errors:
                        raise errors[0]
                    task = asyncio.create_task(send(index, f.read(PART_SIZE)))
                    task.add_done_callback(done)
                    tasks.append(task)
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return InputFileBig(file_id, parts, filename)

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
        try:
            handle = await self._parallel_upload(path, filename, on_progress)
        except Exception as exc:  # noqa: BLE001
            log.warning("parallel yuklash ishlamadi (%s), oddiy usulga o'tilmoqda", type(exc).__name__)
            handle = await self.client.upload_file(
                str(path), part_size_kb=512, file_name=filename, progress_callback=on_progress
            )
        await self.client.send_file(
            entity,
            handle,
            caption=caption,
            parse_mode="html",
            supports_streaming=True,
            mime_type="video/mp4",
            attributes=[
                DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=int(width or 0),
                    h=int(height or 0),
                    supports_streaming=True,
                ),
                DocumentAttributeFilename(filename),
            ],
        )
