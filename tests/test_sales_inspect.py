from copy import deepcopy

import pytest

from stocking_sheet_sync.sales_inspect import _lark_read, read_sheet
from stocking_sheet_sync.sheet_matching import column_name
from tests.test_sales_matching import snapshot


def test_sheet_read_rejects_version_change(monkeypatch):
    data = snapshot()
    calls = []

    def read(command, args):
        calls.append(command)
        if command == "+workbook-info":
            return {
                "data": {
                    "title": "测试表",
                    "revision": calls.count(command),
                    "sheets": [
                        {
                            "sheet_id": "test",
                            "row_count": 7,
                            "column_count": 14,
                        }
                    ],
                }
            }
        if command == "+sheet-info":
            return {"data": {"merged_cells": [{"range": "E2:N2"}]}}
        rows = list(range(1, 8))
        cols = [column_name(c) for c in range(1, 15)]
        return {
            "ok": True,
            "data": {
                "has_more": False,
                "ranges": [
                    {
                        "actual_range": "A1:N7",
                        "row_indices": rows,
                        "col_indices": cols,
                        "cells": [[deepcopy(data["cells"][f"{c}{r}"]) for c in cols] for r in rows],
                        "truncated": False,
                    }
                ],
            },
        }

    monkeypatch.setattr("stocking_sheet_sync.sales_inspect._lark_read", read)
    with pytest.raises(RuntimeError, match="读取期间"):
        read_sheet("test-token", "test")
    assert set(calls) == {"+workbook-info", "+cells-get", "+sheet-info"}


def test_inspection_cannot_call_write_command():
    with pytest.raises(ValueError, match="只支持"):
        _lark_read("+cells-set", [])
