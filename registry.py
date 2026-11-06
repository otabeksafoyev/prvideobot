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
        self.data: dict = {"counter": 0, "codes": {}, "usage": {}, "videos": 0}
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

    def over_budget(self, limit_gb: float) -> bool:
        return limit_gb > 0 and self.usage_bytes() >= limit_gb * 1024**3
