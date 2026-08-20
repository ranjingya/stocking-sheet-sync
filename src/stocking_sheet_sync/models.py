from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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


SyncStatus = Literal["success", "error", "baseline"]


@dataclass(slots=True)
class SyncState:
    record_id: str
    source_token: str
    source_revision: int
    original_name: str
    record_url: str
    target_token: str | None = None
    target_name: str | None = None
    target_url: str | None = None
    copied_at: str | None = None
    status: SyncStatus = "error"
    pending_notify_open_ids: list[str] = field(default_factory=list)
    last_error: str | None = None
    last_error_notified_at: str | None = None
    updated_at: str = ""


@dataclass(slots=True)
class SyncSummary:
    scanned: int = 0
    copied: int = 0
    unchanged: int = 0
    baselined: int = 0
    failed: int = 0
    notifications_retried: int = 0
