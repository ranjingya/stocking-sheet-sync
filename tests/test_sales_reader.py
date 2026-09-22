from datetime import date, timedelta
from unittest.mock import Mock

import pymysql
import pytest

from stocking_sheet_sync.infrastructure.warehouse import SalesReader, units
from stocking_sheet_sync.source_settings import WarehouseSettings, identifier
from tests.test_sales_matching import config


def reader():
    return SalesReader(WarehouseSettings("example.invalid", 3306, "test", "test", "private-secret"))


def test_detail_query_binds_values_deduplicates_keys_and_uses_shipping_window(monkeypatch):
    client = reader()
    platform = next(p for p in config()["platforms"] if p["id"] == "jd_pop")
    calls = []
    responses = [
        [{"latest": "2026-09-04 23:59:59"}],
        [{"day": date(2026, 8, 6) + timedelta(days=n), "n": 1} for n in range(30)],
        [
            {
                "sku": "00123",
                "quantity": 5,
                "n": 4,
                "fact_count": 2,
                "invalid_count": 0,
                "conflicts": 0,
            }
        ],
    ]

    def read(sql, params):
        calls.append((sql, params))
        return responses.pop(0)

    monkeypatch.setattr(client, "_read", read)
    result = client.sales(platform, ["00123"], date(2026, 9, 5))
    assert result["start"] == "2026-08-06" and result["end"] == "2026-09-04"
    assert result["issues"] == []
    assert result["rows"][0]["quantity"] == 5
    assert result["rows"][0]["deduplicated_rows"] == 2
    sql, params = calls[-1]
    assert "`outstock_time` >= %s" in sql and "`outstock_time` < %s" in sql
    assert "GROUP BY `data_source`, `company`, `outstock_order_id`" in sql
    assert "COUNT(DISTINCT `outstock_num`) > 1" in sql
    assert "10537352" not in sql and "00123" not in sql
    assert params[-3:] == ("2026-08-06", "2026-09-05", "00123")
    assert "已出库" in params and "销售出库" in params and "10537352" in params


def test_snapshot_requires_exact_requested_end_date(monkeypatch):
    client = reader()
    responses = [[{"latest": "2026-09-11"}], []]
    calls = []

    def read(sql, params):
        calls.append((sql, params))
        return responses.pop(0)

    monkeypatch.setattr(client, "_read", read)
    result = client.sales(config()["platforms"][0], ["00123"], date(2026, 9, 14))
    assert result["issues"] == ["snapshot_or_skus_missing"]
    assert calls[-1][1][-3:] == ("2026-09-13", "2026-09-14", "00123")
    assert "outbound_30d" in calls[-1][0] and "transaction_product" not in calls[-1][0]


def test_conflicting_duplicate_facts_produce_no_quantity(monkeypatch):
    client = reader()
    source = config()["platforms"][1]
    responses = [
        [{"latest": "2026-09-04"}],
        [],
        [
            {
                "sku": "00123",
                "quantity": 10,
                "n": 2,
                "fact_count": 1,
                "invalid_count": 0,
                "conflicts": 1,
            }
        ],
    ]
    monkeypatch.setattr(client, "_read", lambda *args: responses.pop(0))
    result = client.sales(source, ["00123"], date(2026, 9, 5))
    assert len(result["missing_dates"]) == 30
    assert result["rows"][0]["status"] == "conflicting_source_detail"
    assert result["rows"][0]["quantity"] is None


def test_connection_is_closed_and_credentials_not_logged(monkeypatch, caplog):
    client = reader()
    connection = Mock()
    cursor = connection.cursor.return_value.__enter__ = Mock()
    connection.cursor.return_value.__exit__ = Mock()
    cursor.return_value.fetchall.return_value = [{"ok": 1}]
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: connection)
    assert client._read("SELECT %s", (1,)) == [{"ok": 1}]
    cursor.return_value.execute.assert_called_once_with("SELECT %s", (1,))
    connection.close.assert_called_once()

    def fail(**kwargs):
        raise pymysql.OperationalError(1045, "private-secret example.invalid")

    monkeypatch.setattr(pymysql, "connect", fail)
    with pytest.raises(RuntimeError) as error:
        client._read("SELECT 1", ())
    assert "private-secret" not in str(error.value) + caplog.text
    assert "example.invalid" not in str(error.value) + caplog.text
    with pytest.raises(ValueError):
        client._read("DELETE FROM table_name", ())


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "NaN", "Infinity"])
def test_invalid_quantities_rejected(value):
    with pytest.raises(ValueError):
        units(value)


@pytest.mark.parametrize("value", ["x.y", "x;DROP TABLE a", "x`", ""])
def test_invalid_identifiers_rejected(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_settings_file_is_read_only_and_env_takes_precedence(tmp_path):
    file = tmp_path / "db.env"
    text = (
        "WAREHOUSE_HOST=example.invalid\nWAREHOUSE_DATABASE=base\n"
        "WAREHOUSE_USER=user\nWAREHOUSE_PASSWORD=private-secret\n"
    )
    file.write_text(text)
    settings = WarehouseSettings.load(file, {"WAREHOUSE_DATABASE": "override"})
    assert settings.database == "override"
    assert "private-secret" not in repr(settings)
    assert file.read_text() == text


def test_detail_deduplication_executes_against_real_sql_engine(monkeypatch):
    import sqlite3

    client = reader()
    source = config()["platforms"][1]
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE dwd_whs_outstock_detail_f (
        data_source TEXT, company TEXT, outstock_order_id TEXT, outstock_order_detail_id TEXT,
        spec_code TEXT, outstock_num REAL, outstock_time TEXT, dept TEXT, shop_id TEXT,
        outstock_type TEXT, outstock_status TEXT, document_status TEXT
    )""")
    first = (
        "api",
        "test",
        "o1",
        "d1",
        "00123",
        2,
        "2026-09-04 10:00:00",
        "市场部其他",
        "10537352",
        "销售出库",
        "已出库",
        "已出库",
    )
    second = (*first[:2], "o2", "d2", "00123", 3, *first[6:])
    insert = "INSERT INTO dwd_whs_outstock_detail_f VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    db.executemany(insert, [first, first, second, second])
    monkeypatch.setattr(
        client,
        "_read",
        lambda sql, params: [
            dict(row) for row in db.execute(sql.replace("%s", "?"), params).fetchall()
        ],
    )
    try:
        result = client.sales(source, ["00123"], date(2026, 9, 5))
        assert result["rows"][0]["quantity"] == 5
        assert result["rows"][0]["deduplicated_rows"] == 2
        db.execute(insert, (*first[:5], 4, *first[6:]))
        result = client.sales(source, ["00123"], date(2026, 9, 5))
        assert result["rows"][0]["status"] == "conflicting_source_detail"
        assert result["rows"][0]["quantity"] is None
    finally:
        db.close()


def test_settings_default_to_current_project_env_only(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".env").write_text(
        "WAREHOUSE_HOST=parent.invalid\nWAREHOUSE_DATABASE=parent\n"
        "WAREHOUSE_USER=user\nWAREHOUSE_PASSWORD=parent-secret\n"
    )
    monkeypatch.chdir(project)
    with pytest.raises(ValueError, match="配置不完整"):
        WarehouseSettings.load(env={})
    (project / ".env").write_text(
        "WAREHOUSE_HOST=own.invalid\nWAREHOUSE_DATABASE=own\n"
        "WAREHOUSE_USER=user\nWAREHOUSE_PASSWORD=own-secret\n"
    )
    settings = WarehouseSettings.load(env={})
    assert settings.host == "own.invalid"
    assert settings.database == "own"


def test_unrelated_database_environment_cannot_supply_warehouse_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="配置不完整"):
        WarehouseSettings.load(
            env={
                "DB_HOST": "unrelated.invalid",
                "DB_NAME": "unrelated",
                "DB_USER": "user",
                "DB_PASSWORD": "unrelated-secret",
            }
        )
