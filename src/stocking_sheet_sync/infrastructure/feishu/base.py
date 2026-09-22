from __future__ import annotations

from urllib.parse import quote

from stocking_sheet_sync.domain.models import BaseRecord


class BaseOperations:
    """飞书多维表记录读取。"""

    def get_base_record(self, record_id: str) -> BaseRecord:
        """
        功能说明：根据记录 ID 读取一条多维表记录，供 Webhook 精确触发同步。

        参数：
            record_id：多维表记录 ID。

        返回值：标准化后的单条多维表记录。
        """
        path = (
            f"/open-apis/bitable/v1/apps/{quote(self.config.base_app_token, safe='')}"
            f"/tables/{quote(self.config.base_table_id, safe='')}"
            f"/records/{quote(record_id, safe='')}"
        )
        data = self._request(
            "GET",
            path,
            params={
                "automatic_fields": "true",
                "user_id_type": "open_id",
                "with_shared_url": "true",
            },
        )
        record = _parse_base_record(data.get("record"))
        if record is None:
            raise RuntimeError(f"多维表接口未返回有效记录：{record_id}")
        self.logger.debug("已读取 Webhook 触发记录：record_id=%s", record.record_id)
        return record


def _parse_base_record(value: object) -> BaseRecord | None:
    if not isinstance(value, dict):
        return None
    record_id = str(value.get("record_id", "")).strip()
    fields = value.get("fields", {})
    if not record_id or not isinstance(fields, dict):
        return None
    return BaseRecord(
        record_id=record_id,
        fields=fields,
        shared_url=str(value.get("shared_url") or value.get("record_url") or "").strip(),
    )
