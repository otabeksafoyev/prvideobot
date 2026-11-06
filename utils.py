from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
VIEWKEY_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def validate_url(url: str, allowed_hosts: tuple[str, ...]) -> str | None:
    """Havola ruxsat etilgan domen va view_video.php?viewkey=... shaklida
    bo'lsa, tozalangan (kuzatuv parametrlarisiz) URL qaytaradi."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.scheme not in ("http", "https"):
        return None
    if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        return None
    if not p.path.rstrip("/").endswith("view_video.php"):
        return None
    key = (parse_qs(p.query).get("viewkey") or [""])[0]
    if not VIEWKEY_RE.fullmatch(key):
        return None
    return f"{p.scheme}://{p.netloc}{p.path}?viewkey={key}"


def viewkey_of(url: str) -> str:
    return (parse_qs(urlparse(url).query).get("viewkey") or [""])[0]


def sanitize(text: str, hosts: tuple[str, ...] = ()) -> str:
    """Matndan URL va domenlarni yashiradi."""
    text = re.sub(r"\b(?:socks\d?[a-z]?|https?)://\S+", "[havola]", text, flags=re.I)
    text = URL_RE.sub("[havola]", text)
    for h in hosts:
        text = re.sub(re.escape(h), "***", text, flags=re.I)
    return DOMAIN_RE.sub("***", text)


class SanitizingFilter(logging.Filter):
    def __init__(self, hosts: tuple[str, ...]):
        super().__init__()
        self.hosts = hosts

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize(record.getMessage(), self.hosts)
            record.args = ()
        except Exception:
            pass
        return True


def fmt_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "noma'lum"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(num: float | int | None) -> str:
    if not num:
        return "noma'lum"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return "noma'lum"


def progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def progress_text(kind: str, pct: float | None, speed: float | None, eta: int | None) -> str:
    lines = [f"ᐅ Yuklanmoqda ({kind})"]
    if pct is not None:
        lines.append(f"{progress_bar(pct)} {pct:.0f}%")
    extra = []
    if speed:
        extra.append(f"{fmt_size(speed)}/s")
    if eta is not None:
        extra.append(f"~{fmt_duration(eta)}")
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(lines)


def safe_filename(title: str) -> str:
    name = re.sub(r"[^\w\- ]+", "", title, flags=re.U).strip().replace(" ", "_")[:60]
    return f"{name or 'video'}.mp4"


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)
