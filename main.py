from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from config import Settings, load_settings
from handlers import router
from registry import Registry
from uploader import TelegramUploader
from utils import SanitizingFilter


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    flt = SanitizingFilter(settings.allowed_hosts)
    for h in logging.getLogger().handlers:
        h.addFilter(flt)


def ffmpeg_ok(settings: Settings) -> bool:
    if settings.ffmpeg_location:
        loc = Path(settings.ffmpeg_location)
        if loc.is_dir():
            return shutil.which("ffmpeg", path=str(loc)) is not None
        return loc.exists()
    return shutil.which("ffmpeg") is not None


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    log = logging.getLogger("main")

    if not ffmpeg_ok(settings):
        log.error("FFmpeg topilmadi. O'rnating yoki FFMPEG_LOCATION ni ko'rsating.")
        sys.exit(1)

    if settings.use_mtproto and importlib.util.find_spec("cryptg") is None:
        log.warning(
            "cryptg o'rnatilmagan: katta fayllarni yuborish JUDA sekin bo'ladi. "
            "Tuzatish: pip install cryptg"
        )

    if importlib.util.find_spec("curl_cffi") is None:
        log.warning(
            "curl_cffi o'rnatilmagan: sayt so'rovlarni rad etishi mumkin (410). "
            'Tuzatish: pip install -U "yt-dlp[default,curl-cffi]"'
        )

    # Oldingi ishdan qolgan vaqtinchalik fayllarni tozalash
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    for p in settings.download_dir.iterdir():
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    if settings.local_api_url:
        api = TelegramAPIServer.from_base(settings.local_api_url, is_local=True)
        session = AiohttpSession(api=api, timeout=900)
    else:
        session = AiohttpSession(timeout=900)

    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(settings.data_dir / "registry.json")

    uploader = None
    if settings.use_mtproto:
        uploader = TelegramUploader(settings)
        await uploader.start()
        log.info("MTProto yuklash yoqildi (2 GB gacha).")

    dp = Dispatcher(
        settings=settings,
        registry=registry,
        uploader=uploader,
        gate=asyncio.Semaphore(settings.max_concurrent),
    )
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot ishga tushdi.")
    try:
        await dp.start_polling(bot)
    finally:
        if uploader is not None:
            await uploader.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
