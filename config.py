from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Obuna tariflari: oy -> narx (so'm)
PLANS: dict[int, int] = {1: 30_000, 2: 50_000, 3: 70_000}


def _csv(name: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    allowed_hosts: tuple[str, ...]
    download_dir: Path
    max_concurrent: int
    max_upload_mb: int
    local_api_url: str | None
    ffmpeg_location: str | None
    log_level: str
    api_id: int | None = None
    api_hash: str | None = None
    ytdlp_proxy: str | None = None
    max_height: int = 1080
    data_dir: Path = Path("data")
    monthly_limit_gb: float = 50.0
    upload_workers: int = 8
    payment_channel_id: int = -1003525618102  # cheklar tushadigan kanal
    video_channel_id: int = -1004343251790  # videolar (sifat + kod) tushadigan kanal
    card_number: str | None = None
    card_owner: str | None = None

    @property
    def use_mtproto(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def max_upload_bytes(self) -> int:
        if self.use_mtproto:  # Telethon (MTProto) orqali ~2 GB gacha
            return 1990 * 1024 * 1024
        return self.max_upload_mb * 1024 * 1024


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    try:
        admins = frozenset(int(x) for x in _csv("ADMIN_IDS"))
    except ValueError:
        sys.exit("ADMIN_IDS faqat raqamlardan iborat bo'lishi kerak.")
    hosts = tuple(h.removeprefix("www.") for h in _csv("ALLOWED_HOSTS"))

    if not token:
        sys.exit("BOT_TOKEN ko'rsatilmagan.")
    if not admins:
        sys.exit("ADMIN_IDS ko'rsatilmagan (bot faqat administratorlar uchun).")
    if not hosts:
        sys.exit("ALLOWED_HOSTS ko'rsatilmagan.")

    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip() or None
    if bool(api_id_raw) != bool(api_hash):
        sys.exit("TG_API_ID va TG_API_HASH ikkalasi ham ko'rsatilishi kerak.")
    try:
        api_id = int(api_id_raw) if api_id_raw else None
    except ValueError:
        sys.exit("TG_API_ID faqat raqam bo'lishi kerak.")

    return Settings(
        bot_token=token,
        admin_ids=admins,
        allowed_hosts=hosts,
        download_dir=Path(os.getenv("DOWNLOAD_DIR", "./downloads")).resolve(),
        max_concurrent=max(1, int(os.getenv("MAX_CONCURRENT", "2"))),
        max_upload_mb=max(1, int(os.getenv("MAX_UPLOAD_MB", "50"))),
        local_api_url=os.getenv("LOCAL_API_URL", "").strip() or None,
        ffmpeg_location=os.getenv("FFMPEG_LOCATION", "").strip() or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        api_id=api_id,
        api_hash=api_hash,
        ytdlp_proxy=os.getenv("YTDLP_PROXY", "").strip() or None,
        max_height=max(144, int(os.getenv("MAX_HEIGHT", "1080"))),
        data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
        monthly_limit_gb=float(os.getenv("MONTHLY_LIMIT_GB", "50")),
        upload_workers=max(1, min(16, int(os.getenv("UPLOAD_WORKERS", "8")))),
        payment_channel_id=int(os.getenv("PAYMENT_CHANNEL_ID", "-1003525618102")),
        video_channel_id=int(os.getenv("VIDEO_CHANNEL_ID", "-1004343251790")),
        card_number=os.getenv("CARD_NUMBER", "").strip() or None,
        card_owner=os.getenv("CARD_OWNER", "").strip() or None,
    )
