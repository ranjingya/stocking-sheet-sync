from __future__ import annotations

import fnmatch


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

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

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def delete(self, key: str) -> int:
        existed = key in self.strings or key in self.hashes
        self.strings.pop(key, None)
        self.hashes.pop(key, None)
        self.expirations.pop(key, None)
        return int(existed)

    def expireat(self, key: str, timestamp: int) -> bool:
        if key not in self.strings:
            return False
        self.expirations[key] = timestamp
        return True

    def scan_iter(self, *, match: str):
        for key in list(self.strings):
            if fnmatch.fnmatch(key, match):
                yield key

    def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        del script, key_count
        if self.strings.get(key) != token:
            return 0
        del self.strings[key]
        return 1

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> int:
        target = self.hashes.setdefault(key, {})
        if mapping:
            target.update(mapping)
            return len(mapping)
        if field is None or value is None:
            raise ValueError("field 和 value 不能为空")
        target[field] = value
        return 1

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hexists(self, key: str, field: str) -> int:
        return int(field in self.hashes.get(key, {}))

    def exists(self, key: str) -> int:
        return int(key in self.strings or key in self.hashes)

    def close(self) -> None:
        return None
