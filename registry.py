from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Registry:
    """Video kodlari (50xx) va oylik trafik hisobi. Havola/viewkey saqlanmaydi,
    faqat uning qisqa xeshi."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"counter": 0, "codes": {}, "usage": {}, "videos": 0,
                     "subs": {}, "pending": {}, "posted": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self.data:
                if k in raw:
                    self.data[k] = raw[k]
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            log.warning("registry fayli o'qilmadi, yangidan boshlanadi")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            log.warning("registry saqlanmadi")

    @staticmethod
    def _key(viewkey: str) -> str:
        return hashlib.sha1(viewkey.encode()).hexdigest()[:16]

    def code_for(self, viewkey: str) -> str:
        key = self._key(viewkey)
        n = self.data["codes"].get(key)
        if n is None:
            self.data["counter"] += 1
            n = self.data["counter"]
            self.data["codes"][key] = n
            self._save()
        return f"50{n:02d}"

    @staticmethod
    def _month() -> str:
        return time.strftime("%Y-%m", time.gmtime())

    def usage_bytes(self) -> int:
        return int(self.data["usage"].get(self._month(), 0))

    def video_count(self) -> int:
        return int(self.data["videos"])

    def add_usage(self, nbytes: int) -> None:
        m = self._month()
        self.data["usage"][m] = int(self.data["usage"].get(m, 0)) + int(nbytes)
        self.data["videos"] += 1
        self._save()

    # ───────────── Obunalar ─────────────
    def is_active(self, uid: int) -> bool:
        return float(self.data["subs"].get(str(uid), 0)) > time.time()

    def expiry(self, uid: int) -> float:
        return float(self.data["subs"].get(str(uid), 0))

    def grant(self, uid: int, days: int) -> float:
        base = max(time.time(), self.expiry(uid))
        exp = base + days * 86400
        self.data["subs"][str(uid)] = exp
        self._save()
        return exp

    def revoke(self, uid: int) -> bool:
        existed = self.data["subs"].pop(str(uid), None) is not None
        if existed:
            self._save()
        return existed

    def active_subs(self) -> list[tuple[int, float]]:
        now = time.time()
        items = [(int(u), float(t)) for u, t in self.data["subs"].items() if float(t) > now]
        return sorted(items, key=lambda x: x[1])

    # ───────────── Tanlangan tarif (chek kutilmoqda) ─────────────
    def set_pending(self, uid: int, months: int) -> None:
        self.data["pending"][str(uid)] = months
        self._save()

    def pending(self, uid: int) -> int | None:
        return self.data["pending"].get(str(uid))

    def clear_pending(self, uid: int) -> None:
        if self.data["pending"].pop(str(uid), None) is not None:
            self._save()

    # ───────────── Kanalga bir marta tashlash ─────────────
    def was_posted(self, viewkey: str) -> bool:
        return self._key(viewkey) in self.data["posted"]

    def mark_posted(self, viewkey: str) -> None:
        self.data["posted"][self._key(viewkey)] = 1
        self._save()

    def over_budget(self, limit_gb: float) -> bool:
        return limit_gb > 0 and self.usage_bytes() >= limit_gb * 1024**3
