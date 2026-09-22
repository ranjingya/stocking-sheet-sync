from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import httpx

from stocking_sheet_sync.infrastructure.feishu.sheets import read_sheet
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.layout import (
    apply_update,
    build_update,
    verify_update,
)
from stocking_sheet_sync.settings import load_layout_config, load_sales_config

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：预览或执行新品及老品近30天补列，保存前后快照并进行回读核验。

    参数：
        argv：命令行参数；默认读取进程参数。

    返回值：执行或预览成功返回 0，配置、版本冲突或核验失败返回 1。
    """
    parser = argparse.ArgumentParser(description="预览或执行市场部新品及老品近30天销量补列")
    parser.add_argument("--spreadsheet-token", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    if args.apply and args.expected_revision is None:
        parser.error("执行必须指定 --expected-revision，使用预览读取到的版本")
    configure_logging("INFO")

    def save(name, data):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        config = load_sales_config(args.config)
        rules = load_layout_config(args.config, config)
        before = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        if args.apply and before["revision"] != args.expected_revision:
            raise ValueError("表格版本与指定版本不一致，请重新预览")
        update = build_update(before, config, rules)
        save("before.json", before)
        save("request.json", update)
        if not update["operations"]:
            save("result.json", {"status": "unchanged", "revision": before["revision"]})
            LOG.info("表头已符合规则，无需插列或写入")
            return 0
        if not args.apply:
            LOG.info("执行预览完成：revision=%s output=%s", before["revision"], args.output)
            return 0
        save(
            "response.json",
            apply_update(
                args.spreadsheet_token,
                args.sheet_id,
                update,
                before["revision"],
                on_progress=lambda journal: save("journal.json", journal),
            ),
        )
        after = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        save("after.json", after)
        result = verify_update(before, after, update, config, rules)
        save("result.json", result)
        LOG.info("市场部补列完成并通过回读核验：inserted=%d", len(update["inserted_columns"]))
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, httpx.TransportError) as error:
        LOG.error("市场部补列未完成：%s", error)
        save("error.json", {"error": str(error)})
        return 1


def main() -> None:
    raise SystemExit(run())
