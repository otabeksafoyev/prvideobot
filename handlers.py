from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import PLANS, Settings
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


# ───────────── Ruxsat ─────────────
class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return bool(event.from_user and event.from_user.id in settings.admin_ids)


def fmt_price(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_date(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.gmtime(ts + 5 * 3600))  # UTC+5


def plans_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"ᐅ {m} oy — {fmt_price(p)} so'm", callback_data=f"plan:{m}")]
        for m, p in PLANS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paywall_text() -> str:
    lines = ["ᐅ <b>Obuna kerak</b>", "", "Botdan foydalanish uchun obuna sotib oling:", ""]
    lines += [f"๏ {m} oylik — {fmt_price(p)} so'm" for m, p in PLANS.items()]
    lines += ["", "Tarifni tanlang:"]
    return "\n".join(lines)


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
async def cmd_start(message: Message, settings: Settings, registry: Registry):
    uid = message.from_user.id
    is_admin = uid in settings.admin_ids
    if not is_admin and not registry.is_active(uid):
        await message.answer(paywall_text(), reply_markup=plans_kb())
        return
    text = (
        "ᐅ <b>Private Video Downloader</b>\n\n"
        "Video sahifasining to'liq havolasini yuboring:\n"
        "<code>https://.../view_video.php?viewkey=...</code>\n\n"
        "Bot eng yuqori sifatni o'zi tanlaydi, video MP4 holida yuboriladi va unga kod beriladi."
    )
    if is_admin:
        text += "\n/admin — boshqaruv paneli\n/stat — oylik statistika"
    else:
        text += f"\n\n✓ Obuna: {fmt_date(registry.expiry(uid))} gacha"
    await message.answer(text)


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"ID: <code>{message.from_user.id}</code>")


# ───────────── Havola qabul qilish ─────────────
@router.message(Command("stat"), AdminFilter())
async def cmd_stat(message: Message, settings: Settings, registry: Registry):
    limit = f"{settings.monthly_limit_gb:g} GB" if settings.monthly_limit_gb > 0 else "cheklanmagan"
    await message.answer(
        f"๏ Shu oy yuborilgan: {fmt_size(registry.usage_bytes())} / {limit}\n"
        f"Jami videolar: {registry.video_count()}"
    )


# ───────────── Obuna sotib olish ─────────────
@router.callback_query(F.data == "plans")
async def on_plans(cb: CallbackQuery):
    await cb.answer()
    await safe_edit(cb.message, paywall_text(), reply_markup=plans_kb())


@router.callback_query(F.data.startswith("plan:"))
async def on_plan(cb: CallbackQuery, settings: Settings, registry: Registry):
    try:
        months = int(cb.data.split(":")[1])
    except ValueError:
        months = 0
    price = PLANS.get(months)
    if not price:
        await cb.answer()
        return
    registry.set_pending(cb.from_user.id, months)
    lines = [f"ᐅ <b>Tanlandi: {months} oy — {fmt_price(price)} so'm</b>", ""]
    if settings.card_number:
        lines.append(f"Karta: <code>{esc(settings.card_number)}</code>")
    if settings.card_owner:
        lines.append(f"Egasi: {esc(settings.card_owner)}")
    lines += [
        "",
        "To'lovni qiling va chekni skrinshot (rasm) qilib shu yerga yuboring. "
        "Admin tekshirib, obunangizni yoqadi.",
    ]
    back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Tarifni o'zgartirish", callback_data="plans")]]
    )
    await cb.answer()
    await safe_edit(cb.message, "\n".join(lines), reply_markup=back)


@router.message(F.photo, F.chat.type == "private")
async def on_receipt(message: Message, settings: Settings, registry: Registry):
    user = message.from_user
    if user.id in settings.admin_ids:
        return
    months = registry.pending(user.id)
    price = PLANS.get(months or 0)
    if not months or not price:
        await message.answer(paywall_text(), reply_markup=plans_kb())
        return

    uname = f"@{user.username}" if user.username else "—"
    caption = (
        "ᐅ <b>Yangi to'lov</b>\n"
        f"Foydalanuvchi: {esc(user.full_name)} ({esc(uname)})\n"
        f"ID: <code>{user.id}</code>\n"
        f"Tarif: {months} oy — {fmt_price(price)} so'm"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✓ Tasdiqlash", callback_data=f"pay:ok:{user.id}:{months}"),
            InlineKeyboardButton(text="✗ Rad etish", callback_data=f"pay:no:{user.id}"),
        ]]
    )
    try:
        # Chek faqat to'lov kanaliga yuboriladi
        await message.bot.send_photo(
            chat_id=settings.payment_channel_id,
            photo=message.photo[-1].file_id,
            caption=caption,
            reply_markup=kb,
        )
    except Exception:
        log.exception("Chekni kanalga yuborib bo'lmadi")
        await message.answer("✗ Chekni yuborib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
        return
    registry.clear_pending(user.id)
    await message.answer("✓ Chek qabul qilindi. Admin tekshirib, obunani yoqadi — natija haqida xabar keladi.")


@router.callback_query(F.data.startswith("pay:"), AdminFilter())
async def on_pay_decision(cb: CallbackQuery, registry: Registry):
    parts = cb.data.split(":")
    try:
        act, uid = parts[1], int(parts[2])
        months = int(parts[3]) if act == "ok" else 0
    except (IndexError, ValueError):
        await cb.answer()
        return

    current = cb.message.html_text if cb.message else ""
    if "Tasdiqlandi" in current or "Rad etildi" in current:
        await cb.answer("Bu to'lov allaqachon ko'rib chiqilgan.", show_alert=True)
        return

    admin = esc(cb.from_user.full_name)
    if act == "ok" and months in PLANS:
        exp = registry.grant(uid, months * 30)
        mark = f"✓ Tasdiqlandi — {admin}\nObuna: {fmt_date(exp)} gacha"
        notify = f"✓ To'lov tasdiqlandi. Obunangiz {fmt_date(exp)} gacha yoqildi.\nVideo havolasini yuborishingiz mumkin."
    else:
        mark = f"✗ Rad etildi — {admin}"
        notify = "✗ To'lov tasdiqlanmadi. Chek noto'g'ri bo'lsa, /start orqali qaytadan urinib ko'ring."

    with contextlib.suppress(Exception):
        await cb.message.edit_caption(caption=f"{current}\n\n{mark}", reply_markup=None)
    with contextlib.suppress(Exception):
        await cb.bot.send_message(uid, notify)
    await cb.answer("Bajarildi.")


# ───────────── Admin panel ─────────────
def admin_text(settings: Settings, registry: Registry) -> str:
    limit = f"{settings.monthly_limit_gb:g} GB" if settings.monthly_limit_gb > 0 else "cheklanmagan"
    return (
        "ᐅ <b>Admin panel</b>\n\n"
        f"๏ Faol obunachilar: {len(registry.active_subs())}\n"
        f"๏ Jami videolar: {registry.video_count()}\n"
        f"๏ Shu oy trafik: {fmt_size(registry.usage_bytes())} / {limit}\n\n"
        "/grant ID KUN — obuna berish\n"
        "/revoke ID — obunani olib tashlash\n"
        "/subs — obunachilar ro'yxati\n"
        "/stat — statistika"
    )


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="ᐅ Obunachilar", callback_data="adm:subs"),
            InlineKeyboardButton(text="ᐅ Yangilash", callback_data="adm:refresh"),
        ]]
    )


def subs_text(registry: Registry) -> str:
    subs = registry.active_subs()
    if not subs:
        return "Faol obunachilar yo'q."
    lines = [f"ᐅ <b>Faol obunachilar: {len(subs)}</b>", ""]
    for uid, exp in subs[:40]:
        lines.append(f'๏ <a href="tg://user?id={uid}">{uid}</a> — {fmt_date(exp)} gacha')
    if len(subs) > 40:
        lines.append(f"… va yana {len(subs) - 40} ta")
    return "\n".join(lines)


@router.message(Command("admin"), AdminFilter())
async def cmd_admin(message: Message, settings: Settings, registry: Registry):
    await message.answer(admin_text(settings, registry), reply_markup=admin_kb())


@router.callback_query(F.data.startswith("adm:"), AdminFilter())
async def on_admin_cb(cb: CallbackQuery, settings: Settings, registry: Registry):
    await cb.answer()
    if cb.data == "adm:subs":
        await cb.message.answer(subs_text(registry))
    else:
        await safe_edit(cb.message, admin_text(settings, registry), reply_markup=admin_kb())


@router.message(Command("subs"), AdminFilter())
async def cmd_subs(message: Message, registry: Registry):
    await message.answer(subs_text(registry))


@router.message(Command("grant"), AdminFilter())
async def cmd_grant(message: Message, registry: Registry):
    args = (message.text or "").split()[1:]
    try:
        uid, days = int(args[0]), int(args[1])
        if days <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Foydalanish: <code>/grant ID KUN</code>\nMasalan: <code>/grant 123456789 30</code>")
        return
    exp = registry.grant(uid, days)
    await message.answer(f"✓ <code>{uid}</code> uchun obuna {fmt_date(exp)} gacha yoqildi.")
    with contextlib.suppress(Exception):
        await message.bot.send_message(uid, f"✓ Obunangiz {fmt_date(exp)} gacha yoqildi.")


@router.message(Command("revoke"), AdminFilter())
async def cmd_revoke(message: Message, registry: Registry):
    args = (message.text or "").split()[1:]
    try:
        uid = int(args[0])
    except (IndexError, ValueError):
        await message.answer("Foydalanish: <code>/revoke ID</code>")
        return
    if registry.revoke(uid):
        await message.answer(f"✓ <code>{uid}</code> obunasi o'chirildi.")
    else:
        await message.answer("Bu foydalanuvchida faol obuna yo'q.")


@router.message(F.text)
async def on_link(
    message: Message,
    settings: Settings,
    gate: asyncio.Semaphore,
    uploader: TelegramUploader | None,
    registry: Registry,
):
    uid = message.from_user.id
    if uid not in settings.admin_ids and not registry.is_active(uid):
        await message.answer(paywall_text(), reply_markup=plans_kb())
        return

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

            sent_id = await uploader.send_video(
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
            sent = await message.bot.send_video(
                chat_id=status.chat.id,
                video=FSInputFile(result.path, filename=safe_filename(result.title)),
                caption=caption,
                duration=result.duration or None,
                width=result.width,
                height=result.height,
                supports_streaming=True,
                request_timeout=900,
            )
            sent_id = sent.message_id

        registry.add_usage(size)

        # Video kanalga faqat 1 marta tashlanadi (sifat + kod bilan). Qayta yuklanmaydi: nusxa olinadi.
        vkey = viewkey_of(url)
        if not registry.was_posted(vkey):
            try:
                await message.bot.copy_message(
                    chat_id=settings.video_channel_id,
                    from_chat_id=status.chat.id,
                    message_id=sent_id,
                    caption=f"Sifat: {result.height or height}p\nKod: {code}",
                )
                registry.mark_posted(vkey)
            except Exception:
                log.exception("Videoni kanalga tashlab bo'lmadi")
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
