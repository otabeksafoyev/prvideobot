from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from downloader import VideoInfo


@dataclass
class Pending:
    url: str
    info: VideoInfo
    user_id: int
    created: float = field(default_factory=time.monotonic)
    busy: bool = False


class PendingStore:
    """URL'lar faqat xotirada saqlanadi (callback_data ichida emas) va TTL bilan o'chadi."""

    def __init__(self, ttl: int = 900):
        self.ttl = ttl
        self._items: dict[str, Pending] = {}

    def _gc(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if now - v.created > self.ttl and not v.busy]:
            del self._items[k]

    def add(self, url: str, info: VideoInfo, user_id: int) -> str:
        self._gc()
        token = secrets.token_urlsafe(6)
        self._items[token] = Pending(url, info, user_id)
        return token

    def get(self, token: str, user_id: int) -> Pending | None:
        self._gc()
        item = self._items.get(token)
        return item if item and item.user_id == user_id else None

    def pop(self, token: str) -> None:
        self._items.pop(token, None)
