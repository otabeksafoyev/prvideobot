from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import time

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import Settings
from downloader import UserFacingError, download_video
from registry import Registry
from uploader import TelegramUploader
from utils import (
    esc,
    extract_url,
    fmt_duration,
    fmt_size,
    progress_bar,
    safe_filename,
    validate_url,
    viewkey_of,
)

log = logging.getLogger(__name__)
router = Router()


# ───────────── Ruxsat (faqat administratorlar) ─────────────
class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id in self.admin_ids:
            return await handler(event, data)

        # /id hamma uchun ochiq — administrator o'z ID'sini bilib olishi uchun
        if isinstance(event, Message) and event.text:
            if event.text.split()[0].split("@")[0].lower() == "/id":
                return await handler(event, data)
            await event.answer("Bu bot shaxsiy. Ruxsat yo'q.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Ruxsat yo'q.", show_alert=True)
        return None


async def safe_edit(msg: Message, text: str, reply_markup=None, retries: int = 4) -> None:
    """VPN/tarmoq uzilganda ham xabarni yangilashga bir necha marta urinadi."""
    for i in range(retries):
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramNetworkError:
            await asyncio.sleep(1.5 * (i + 1))
        except (TelegramBadRequest, TelegramRetryAfter):
            return
    log.warning("Telegram bilan aloqa uzildi: xabarni yangilab bo'lmadi")


# ───────────── Progress yangilash (throttle) ─────────────
class ProgressReporter:
    def __init__(self, message: Message, loop: asyncio.AbstractEventLoop, interval: float = 2.5):
        self.message = message
        self.loop = loop
        self.interval = interval
        self._last_text = ""
        self._last_time = 0.0

    def __call__(self, text: str, force: bool = False) -> None:  # ishchi oqimdan
        now = time.monotonic()
        if text == self._last_text or (not force and now - self._last_time < self.interval):
            return
        self._last_text, self._last_time = text, now
        asyncio.run_coroutine_threadsafe(self._edit(text), self.loop)

    async def _edit(self, text: str) -> None:
        with contextlib.suppress(TelegramBadRequest, TelegramRetryAfter):
            await self.message.edit_text(text)


# ───────────── Buyruqlar ─────────────
@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "ᐅ <b>Private Video Downloader</b>\n\n"
        "Video sahifasining to'liq havolasini yuboring:\n"
        "<code>https://.../view_video.php?viewkey=...</code>\n\n"
        "Bot eng yuqori sifatni o'zi tanlaydi, video MP4 holida yuboriladi va unga kod beriladi.\n"
        "/stat — oylik statistika"
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"ID: <code>{message.from_user.id}</code>")


# ───────────── Havola qabul qilish ─────────────
@router.message(Command("stat"))
async def cmd_stat(message: Message, settings: Settings, registry: Registry):
    limit = f"{settings.monthly_limit_gb:g} GB" if settings.monthly_limit_gb > 0 else "cheklanmagan"
    await message.answer(
        f"๏ Shu oy yuborilgan: {fmt_size(registry.usage_bytes())} / {limit}\n"
        f"Jami videolar: {registry.video_count()}"
    )


@router.message(F.text)
async def on_link(
    message: Message,
    settings: Settings,
    gate: asyncio.Semaphore,
    uploader: TelegramUploader | None,
    registry: Registry,
):
    raw = extract_url(message.text)
    if not raw:
        await message.answer("Video sahifasining havolasini yuboring.")
        return

    url = validate_url(raw, settings.allowed_hosts)
    if not url:
        await message.answer("Havola noto'g'ri yoki bu manba qo'llab-quvvatlanmaydi.")
        return

    if registry.over_budget(settings.monthly_limit_gb):
        await message.answer("✗ Oylik trafik limiti tugadi. /stat ni ko'ring yoki MONTHLY_LIMIT_GB ni oshiring.")
        return

    # Chatda havola qolib ketmasligi uchun foydalanuvchi xabarini o'chiramiz
    with contextlib.suppress(Exception):
        await message.delete()

    status = await message.answer("ᐅ Boshlanmoqda…")
    limit = settings.max_upload_bytes
    job_dir = settings.download_dir / f"{message.from_user.id}_{secrets.token_hex(4)}"
    loop = asyncio.get_running_loop()

    try:
        height = settings.max_height
        t_start = time.monotonic()
        async with gate:
            reporter = ProgressReporter(status, loop)
            for _ in range(3):
                result = await asyncio.to_thread(
                    download_video, url, height, job_dir, settings, reporter
                )
                size = result.path.stat().st_size
                if size <= limit:
                    break
                # Telegram limitidan katta: bir pog'ona pastroq sifatga tushamiz
                shutil.rmtree(job_dir, ignore_errors=True)
                lower = [h for h in (720, 480, 360) if h < (result.height or height)]
                if not lower:
                    raise UserFacingError("Fayl hajmi Telegram limitidan katta.")
                height = lower[0]
                await safe_edit(status, f"ᐅ Fayl katta, {height}p ga tushirilmoqda…")
            else:
                raise UserFacingError("Fayl hajmi Telegram limitidan katta.")
        t_download = time.monotonic() - t_start

        code = registry.code_for(viewkey_of(url))
        caption = (
            f"<b>{esc(result.title[:200])}</b>\n"
            f"Davomiyligi: {fmt_duration(result.duration)}\n"
            f"Sifat: {result.height or height}p\n"
            f"Kod: {code}"
        )
        await safe_edit(status, "ᐅ Telegramga yuborilmoqda…")

        if uploader is not None:
            last = {"t": 0.0}
            started = time.monotonic()

            async def upload_progress(sent: int, total: int) -> None:
                now = time.monotonic()
                if sent < total and now - last["t"] < 2.5:
                    return
                last["t"] = now
                pct = sent / total * 100 if total else 0.0
                speed = sent / max(now - started, 0.1)
                eta = (total - sent) / speed if speed > 0 else None
                text = f"ᐅ Telegramga yuborilmoqda\n{progress_bar(pct)} {pct:.0f}%"
                text += f"\n{fmt_size(speed)}/s"
                if eta is not None and sent < total:
                    text += f" · ~{fmt_duration(eta)}"
                # Progress xabari xato bersa ham yuborish to'xtab qolmasin
                with contextlib.suppress(Exception):
                    await status.edit_text(text)

            await uploader.send_video(
                chat_id=status.chat.id,
                username=message.from_user.username,
                path=result.path,
                caption=caption,
                duration=result.duration,
                width=result.width,
                height=result.height,
                filename=safe_filename(result.title),
                on_progress=upload_progress,
            )
        else:
            await message.bot.send_video(
                chat_id=status.chat.id,
                video=FSInputFile(result.path, filename=safe_filename(result.title)),
                caption=caption,
                duration=result.duration or None,
                width=result.width,
                height=result.height,
                supports_streaming=True,
                request_timeout=900,
            )

        registry.add_usage(size)
        t_upload = time.monotonic() - t_start - t_download
        log.info(
            "vaqt: yuklash %.0fs (%s/s), yuborish %.0fs (%s/s), hajm %s, kod %s",
            t_download, fmt_size(size / max(t_download, 0.1)),
            t_upload, fmt_size(size / max(t_upload, 0.1)),
            fmt_size(size), code,
        )
        with contextlib.suppress(Exception):
            await status.delete()

    except UserFacingError as e:
        await safe_edit(status, f"✗ {esc(str(e))}")
    except Exception:
        log.exception("Kutilmagan xato")
        await safe_edit(status, "✗ Kutilmagan xatolik yuz berdi.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)  # vaqtinchalik fayllar
