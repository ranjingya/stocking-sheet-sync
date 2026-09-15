from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SyncResult = Literal["copied", "unchanged", "skipped", "busy", "failed"]


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
class CopyResult:
    name: str
    token: str
    file_type: str
    url: str


@dataclass(frozen=True, slots=True)
class CopyState:
    record_id: str
    source_token: str
    source_name: str
    source_url: str
    record_url: str
    status: Literal["copying", "copied"]
    attempt_id: str = ""
    started_at: str = ""
    target_token: str = ""
    target_name: str = ""
    target_url: str = ""
    copied_at: str = ""
    request_id: str = ""
    workflow: str = "single"
    backup_folder_token: str = ""
    delivery_folder_token: str = ""
    history_enabled: bool = False
    forecast_enabled: bool = False
    backup_name: str = ""


@dataclass(slots=True)
class SyncSummary:
    scanned: int = 0
    copied: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    result: SyncResult = "unchanged"
    reason: str = ""
    history_status: str = "disabled"
    forecast_status: str = "disabled"
    target_url: str = ""
    fill_report_path: str = ""
    original_backup_url: str = ""
    filled_backup_url: str = ""
    delivery_source: str = ""
    fill_degraded: bool = False
    force: bool = False
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class FillState:
    record_id: str
    source_token: str
    target_token: str
    as_of: str
    attempt_id: str
    status: Literal["running", "completed", "retryable", "needs_review"] = "running"
    reason: str = ""
    report_path: str = ""


@dataclass(frozen=True, slots=True)
class CopyStep:
    stage: str
    source_token: str
    folder_token: str
    name: str
    attempt_id: str
    status: Literal["copying", "copied"] = "copying"
    target_token: str = ""
    target_url: str = ""
    copied_at: str = ""
