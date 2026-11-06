from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadError, UnsupportedError

from config import Settings
from utils import progress_text

log = logging.getLogger(__name__)

SUPPORTED_HEIGHTS = (360, 480, 720, 1080)

# on_status(text, force) — ishchi oqimdan chaqiriladi
StatusCb = Callable[[str, bool], None]


class UserFacingError(Exception):
    """Foydalanuvchiga ko'rsatilishi xavfsiz (domen/URL yo'q) xabar."""


@dataclass
class VideoInfo:
    title: str
    duration: int
    qualities: dict[int, int | None]  # balandlik -> taxminiy hajm (bayt)


@dataclass
class DownloadResult:
    path: Path
    title: str
    duration: int
    width: int | None
    height: int | None


def _friendly(exc: BaseException) -> str:
    inner = exc
    if isinstance(exc, DownloadError) and exc.exc_info and exc.exc_info[1]:
        inner = exc.exc_info[1]
    msg = str(exc).lower()

    if isinstance(inner, UnsupportedError) or "unsupported url" in msg:
        return "Bu havola qo'llab-quvvatlanmaydi."
    if "drm" in msg:
        return "Video himoyalangan (DRM), uni yuklab bo'lmaydi."
    if any(k in msg for k in ("403", "forbidden", "cloudflare", "access denied", "captcha")):
        return "Sayt so'rovni rad etdi (403). Tarmoq/VPN yoki yt-dlp versiyasini tekshiring."
    if any(k in msg for k in ("age verification", "verify your age", "confirm your age", "age-restricted", "age restricted")):
        return "Video yosh tasdiqlashni talab qiladi. Uni yuklab bo'lmaydi."
    if any(k in msg for k in ("private video", "this video is private", "log in", "login required", "sign in", "premium", "members only", "password")):
        return "Video yopiq yoki kirish talab qilinadi. Uni yuklab bo'lmaydi."
    if "http error 410" in msg or "410: gone" in msg:
        return "Sayt bu sahifani o'chirilgan deb qaytardi (410). Havolani brauzerda tekshiring yoki boshqa video sinang."
    if any(k in msg for k in ("removed", "not found", "404", "has been deleted", "video unavailable", "disabled")):
        return "Video topilmadi yoki o'chirib yuborilgan."
    if any(k in msg for k in ("geo", "your country", "your region", "not available in")):
        return "Video sizning hududingizda mavjud emas."
    if "requested format" in msg:
        return "Mos format topilmadi."
    if "proxy" in msg or "socks" in msg:
        return "Proksi bilan ulanib bo'lmadi. YTDLP_PROXY sozlamasini tekshiring."
    if "ffmpeg" in msg or "ffprobe" in msg:
        return "FFmpeg bilan xatolik. Server sozlamalarini tekshiring."
    if any(k in msg for k in ("timed out", "timeout", "connection", "network", "temporary failure", "name resolution", "getaddrinfo")):
        return "Tarmoq xatosi. Internet/VPN ni tekshirib, qayta urinib ko'ring."
    if "429" in msg or "too many" in msg:
        return "So'rovlar juda ko'p. Birozdan keyin urinib ko'ring."
    return "Videoni yuklab bo'lmadi."


def _base_opts(settings: Settings) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "logger": _SilentLogger(),
    }
    if settings.ffmpeg_location:
        opts["ffmpeg_location"] = settings.ffmpeg_location
    if settings.ytdlp_proxy:
        opts["proxy"] = settings.ytdlp_proxy
    return opts


class _SilentLogger:
    """yt-dlp xabarlari (ichida URL bo'lishi mumkin) logga tushmasin."""

    def debug(self, msg): ...
    def info(self, msg): ...
    def warning(self, msg): ...
    def error(self, msg): ...


def _fmt_size(f: dict, duration: int) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = int(f["tbr"] * 125 * duration)
    return int(size) if size else None


def fetch_info(url: str, settings: Settings) -> VideoInfo:
    try:
        with yt_dlp.YoutubeDL(_base_opts(settings)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("extract xatosi: %s: %s", type(exc).__name__, str(exc)[:400])
        raise UserFacingError(_friendly(exc)) from None

    if not info:
        raise UserFacingError("Video ma'lumotini olib bo'lmadi.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise UserFacingError("Video topilmadi.")
        info = entries[0]
    if info.get("is_live"):
        raise UserFacingError("Jonli efirni yuklab bo'lmaydi.")

    duration = int(info.get("duration") or 0)
    formats = [f for f in (info.get("formats") or []) if not f.get("has_drm")]

    # Kodek noma'lum (None) bo'lsa ham, balandligi bor format video hisoblanadi
    videos = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio_size = 0
    if audios:
        best_a = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        audio_size = _fmt_size(best_a, duration) or 0

    qualities: dict[int, int | None] = {}
    for height in SUPPORTED_HEIGHTS:
        tier = [f for f in videos if f.get("height") == height]
        if not tier:
            continue
        best = max(tier, key=lambda f: f.get("tbr") or 0)
        size = _fmt_size(best, duration)
        if size and best.get("acodec") in (None, "none"):
            size += audio_size
        qualities[height] = size

    if not qualities:
        # Aniq 360/480/720/1080 bo'lmasa, eng yaqin standartga tushiramiz
        for v in videos:
            h = v.get("height")
            if h and h >= 200:
                closest = min(SUPPORTED_HEIGHTS, key=lambda x: abs(x - h))
                if closest not in qualities:
                    qualities[closest] = _fmt_size(v, duration)

    if not qualities:
        log.warning("mos format yo'q; topilgan balandliklar: %s", sorted({f["height"] for f in videos}))
        raise UserFacingError("360p–1080p oralig'ida mos format topilmadi.")

    return VideoInfo(title=str(info.get("title") or "Video"), duration=duration, qualities=qualities)


def _format_selector(height: int) -> str:
    # Avval H.264 (Telegram'da eng yaxshi ishlaydi), keyin istalgan kodek.
    return (
        f"bv*[height<=?{height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*[height<=?{height}]+ba"
        f"/b[height<=?{height}]"
    )


def download_video(url: str, height: int, out_dir: Path, settings: Settings, on_status: StatusCb) -> DownloadResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        if total:
            pct = min(done / total * 100, 100.0)
        elif d.get("fragment_count"):
            pct = min((d.get("fragment_index") or 0) / d["fragment_count"] * 100, 100.0)
        else:
            pct = None
        is_video = (d.get("info_dict") or {}).get("vcodec") != "none"
        on_status(progress_text("video" if is_video else "audio", pct, d.get("speed"), d.get("eta")), False)

    def pp_hook(d: dict) -> None:
        if d.get("status") == "started":
            on_status("ᐅ Birlashtirilmoqda (FFmpeg, qayta siqilmaydi)…", True)

    opts = _base_opts(settings)
    opts.update(
        {
            "format": _format_selector(height),
            "merge_output_format": "mp4",  # -c copy: qayta siqish yo'q
            "outtmpl": str(out_dir / "video.%(ext)s"),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        }
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("download xatosi: %s: %s", type(exc).__name__, str(exc)[:400])
        raise UserFacingError(_friendly(exc)) from None

    if info and info.get("_type") == "playlist":
        info = (info.get("entries") or [info])[0]

    path: Path | None = None
    for rd in (info or {}).get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and Path(fp).exists():
            path = Path(fp)
            break
    if path is None:
        found = sorted(out_dir.glob("video.mp4")) or sorted(out_dir.glob("*.mp4"))
        path = found[0] if found else None
    if path is None:
        raise UserFacingError("Yuklangan fayl topilmadi.")

    return DownloadResult(
        path=path,
        title=str(info.get("title") or "Video"),
        duration=int(info.get("duration") or 0),
        width=info.get("width"),
        height=info.get("height"),
    )
