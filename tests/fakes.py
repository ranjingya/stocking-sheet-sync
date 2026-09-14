from __future__ import annotations

import fnmatch
import threading


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self._lock = threading.RLock()

    def ping(self) -> bool:
        return True

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        with self._lock:
            if nx and key in self.strings:
                return None
            self.strings[key] = value
            self.expirations.pop(key, None)
            if ex is not None:
                self.expirations[key] = ex
            return True

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.strings.get(key)

    def delete(self, key: str) -> int:
        with self._lock:
            existed = key in self.strings
            self.strings.pop(key, None)
            self.expirations.pop(key, None)
            return int(existed)

    def scan_iter(self, *, match: str):
        for key in list(self.strings):
            if fnmatch.fnmatch(key, match):
                yield key

    def eval(self, script: str, key_count: int, key: str, expected: str, *args: str) -> int:
        with self._lock:
            assert key_count == 1
            if self.strings.get(key) != expected:
                return 0
            if "redis.call('SET'" in script:
                self.set(key, args[0])
                return 1
            return self.delete(key)

    def close(self) -> None:
        return None
