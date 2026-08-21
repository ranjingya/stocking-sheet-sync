from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SyncResult = Literal["copied", "unchanged", "observing", "skipped", "busy", "failed"]


@dataclass(frozen=True, slots=True)
class BaseRecord:
    record_id: str
    fields: dict[str, Any]
    shared_url: str = ""


@dataclass(frozen=True, slots=True)
class SourceSheet:
    token: str
    title: str
    source_url: str
    mention_type: Literal["Wiki", "Sheet"]


@dataclass(frozen=True, slots=True)
class ResolvedSheet:
    token: str
    title: str
    revision: int
    source_url: str


@dataclass(frozen=True, slots=True)
class CopyResult:
    name: str
    token: str
    file_type: str
    url: str


@dataclass(frozen=True, slots=True)
class SyncedRecord:
    record_id: str
    source_token: str
    source_revision: int
    source_name: str
    source_url: str
    record_url: str
    target_name: str
    target_url: str
    synced_at: str
    copy_version: int = 1
    monitor_started_at: str = ""
    monitor_expires_at: str = ""


@dataclass(frozen=True, slots=True)
class SyncedSheetState:
    record_id: str
    source_token: str
    synced_revision: int
    source_name: str
    source_url: str
    record_url: str
    target_name: str
    target_url: str
    synced_at: str
    copy_version: int = 1
    monitor_started_at: str = ""
    monitor_expires_at: str = ""
    pending_revision: int | None = None
    pending_since: str = ""
    versions: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class SyncSummary:
    scanned: int = 0
    copied: int = 0
    unchanged: int = 0
    observing: int = 0
    skipped: int = 0
    failed: int = 0
    result: SyncResult = "unchanged"
    reason: str = ""
