from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Settings
from downloader import UserFacingError, download_video, fetch_info
from store import PendingStore
from uploader import TelegramUploader
from utils import (
    esc,
    extract_url,
    fmt_duration,
    fmt_size,
    progress_bar,
    safe_filename,
    validate_url,
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
        "Bot sifatlarni aniqlaydi, siz tanlaysiz, video MP4 holida yuboriladi."
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"ID: <code>{message.from_user.id}</code>")


# ───────────── Havola qabul qilish ─────────────
@router.message(F.text)
async def on_link(message: Message, settings: Settings, store: PendingStore):
    raw = extract_url(message.text)
    if not raw:
        await message.answer("Video sahifasining havolasini yuboring.")
        return

    url = validate_url(raw, settings.allowed_hosts)
    if not url:
        await message.answer("Havola noto'g'ri yoki bu manba qo'llab-quvvatlanmaydi.")
        return

    # Chatda havola qolib ketmasligi uchun foydalanuvchi xabarini o'chiramiz
    with contextlib.suppress(Exception):
        await message.delete()

    status = await message.answer("ᐅ Tekshirilmoqda…")
    try:
        info = await asyncio.to_thread(fetch_info, url, settings)
    except UserFacingError as e:
        await safe_edit(status, f"✗ {esc(str(e))}")
        return
    except Exception:
        log.exception("fetch_info kutilmagan xato")
        await safe_edit(status, "✗ Kutilmagan xatolik yuz berdi.")
        return

    token = store.add(url, info, message.from_user.id)
    rows, row = [], []
    for h, size in sorted(info.qualities.items()):
        label = f"{h}p · ~{fmt_size(size)}" if size else f"{h}p"
        row.append(InlineKeyboardButton(text=label, callback_data=f"dl:{token}:{h}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✓ Eng yaxshisi (avto)", callback_data=f"dl:{token}:0")])
    rows.append([InlineKeyboardButton(text="✗ Bekor qilish", callback_data=f"cancel:{token}")])

    avail = ", ".join(f"{h}p" for h in sorted(info.qualities))
    await safe_edit(
        status,
        f"๏ <b>{esc(info.title[:200])}</b>\n"
        f"Davomiyligi: {fmt_duration(info.duration)}\n"
        f"Mavjud sifatlar: {avail}\n\n"
        "Sifatni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(cb: CallbackQuery, store: PendingStore):
    item = store.get(cb.data.split(":", 1)[1], cb.from_user.id)
    if item and not item.busy:
        store.pop(cb.data.split(":", 1)[1])
        await cb.message.edit_text("✗ Bekor qilindi.")
    await cb.answer()


# ───────────── Yuklab olish va yuborish ─────────────
@router.callback_query(F.data.startswith("dl:"))
async def on_download(
    cb: CallbackQuery,
    settings: Settings,
    store: PendingStore,
    gate: asyncio.Semaphore,
    uploader: TelegramUploader | None,
):
    try:
        _, token, h = cb.data.split(":")
        height = int(h)
    except ValueError:
        await cb.answer()
        return

    item = store.get(token, cb.from_user.id)
    if not item:
        await cb.answer("Sessiya tugagan. Havolani qayta yuboring.", show_alert=True)
        return
    if item.busy:
        await cb.answer("Allaqachon yuklanmoqda.")
        return

    limit = settings.max_upload_bytes
    q = item.info.qualities
    if height == 0:  # avto: limitga sig'adigan eng yuqori sifat
        fitting = [x for x, s in q.items() if not s or s <= limit]
        height = max(fitting) if fitting else min(q)
    if height not in q:
        await cb.answer("Bu sifat mavjud emas.", show_alert=True)
        return
    est = q[height]
    if est and est > limit * 1.1:
        await cb.answer(
            f"Bu sifat Telegram limitidan katta (~{fmt_size(est)}, limit {fmt_size(limit)}). "
            "Pastroq sifatni tanlang"
            + ("." if settings.use_mtproto else " yoki .env ga TG_API_ID/TG_API_HASH qo'shing (2 GB gacha)."),
            show_alert=True,
        )
        return

    await cb.answer()
    item.busy = True
    msg = cb.message
    job_dir = settings.download_dir / f"{cb.from_user.id}_{token}"
    loop = asyncio.get_running_loop()

    try:
        await msg.edit_text(f"ᐅ Navbatda… ({height}p)")
        t_start = time.monotonic()
        async with gate:
            reporter = ProgressReporter(msg, loop)
            result = await asyncio.to_thread(
                download_video, item.url, height, job_dir, settings, reporter
            )
        t_download = time.monotonic() - t_start

        size = result.path.stat().st_size
        if size > limit:
            raise UserFacingError(
                f"Fayl hajmi ({fmt_size(size)}) Telegram limitidan ({fmt_size(limit)}) katta. "
                "Pastroq sifatni tanlang."
            )

        await msg.edit_text("ᐅ Telegramga yuborilmoqda…")
        caption = (
            f"<b>{esc(result.title[:200])}</b>\n"
            f"Davomiyligi: {fmt_duration(result.duration)}\n"
            f"Sifat: {result.height or height}p\n"
            f"Hajmi: {fmt_size(size)}"
        )
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
                    await msg.edit_text(text)

            await uploader.send_video(
                chat_id=msg.chat.id,
                username=cb.from_user.username,
                path=result.path,
                caption=caption,
                duration=result.duration,
                width=result.width,
                height=result.height,
                filename=safe_filename(result.title),
                on_progress=upload_progress,
            )
        else:
            await cb.bot.send_video(
                chat_id=msg.chat.id,
                video=FSInputFile(result.path, filename=safe_filename(result.title)),
                caption=caption,
                duration=result.duration or None,
                width=result.width,
                height=result.height,
                supports_streaming=True,
                request_timeout=900,
            )
        t_upload = time.monotonic() - t_start - t_download
        log.info(
            "vaqt: yuklash %.0fs (%s/s), yuborish %.0fs (%s/s), hajm %s",
            t_download, fmt_size(size / max(t_download, 0.1)),
            t_upload, fmt_size(size / max(t_upload, 0.1)),
            fmt_size(size),
        )
        with contextlib.suppress(Exception):
            await msg.delete()

    except UserFacingError as e:
        await safe_edit(msg, f"✗ {esc(str(e))}")
    except Exception:
        log.exception("Yuborishda kutilmagan xato")
        await safe_edit(msg, "✗ Kutilmagan xatolik yuz berdi.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)  # vaqtinchalik fayllar
        store.pop(token)  # URL xotiradan o'chadi
