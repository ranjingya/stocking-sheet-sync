from __future__ import annotations

from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

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
        del ex
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        del script, key_count
        if self.strings.get(key) != token:
            return 0
        del self.strings[key]
        return 1

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, *, mapping: dict[str, str]) -> int:
        target = self.hashes.setdefault(key, {})
        target.update(mapping)
        return len(mapping)

    def exists(self, key: str) -> int:
        return int(key in self.strings or key in self.hashes)

    def close(self) -> None:
        return None
