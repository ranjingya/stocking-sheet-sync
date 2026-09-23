"""按平台隔离数量校验问题，保留其他平台的写入计划。"""

import re
from collections import Counter, defaultdict

from stocking_sheet_sync.domain.products import column_number


def isolate_platforms(update: dict, sheet_id: str, *, blocked: dict | None = None) -> dict:
    """
    功能说明：跳过有数据或目标冲突的平台，生成其余平台的数量和合计写入。

    参数：
        update：包含全部候选数量、合计和全局问题的计划。
        sheet_id：目标工作表ID。
        blocked：来源检查已确定需要跳过的平台及原因。
    返回值：隔离后的计划；全局问题或所有平台不可用时保持阻断。
    """
    if update.get("target_issues"):
        return update
    failures = {pid: list(reasons) for pid, reasons in (blocked or {}).items()}
    all_entries = update["entries"]
    all_totals = update.get("total_entries", [])
    for entry in [*all_entries, *all_totals]:
        if entry["status"] == "needs_review":
            pid = entry["platform"].split(":")[0]
            failures.setdefault(pid, []).extend(entry.get("issues") or ["target_conflict"])
    if not failures:
        return update

    def active(entry):
        return entry["platform"].split(":")[0] not in failures

    entries = [entry for entry in all_entries if active(entry)]
    totals = [entry for entry in all_totals if active(entry)]
    operations = []
    quantities = defaultdict(int)
    for entry in entries:
        quantities[entry["platform"]] += entry["quantity"]
        if entry["status"] == "write":
            address = entry["target_cell"]
            operations.append(
                {"range": f"{sheet_id}!{address}:{address}", "values": [[entry["quantity"]]]}
            )
    for entry in totals:
        if entry["status"] == "write":
            address = entry["target_cell"]
            operations.append(
                {
                    "range": f"{sheet_id}!{address}:{address}",
                    "values": [[{"type": "formula", "text": entry["formula"]}]],
                }
            )
    counts = Counter(e["status"] for e in entries)
    return {
        **update,
        "entries": entries,
        "total_entries": totals,
        "skipped_entries": [entry for entry in all_entries if not active(entry)],
        "skipped_total_entries": [entry for entry in all_totals if not active(entry)],
        "blocked_platforms": {pid: sorted(set(reasons)) for pid, reasons in failures.items()},
        "operations": compact_ranges(operations),
        "summary": {
            **update["summary"],
            "needs_review": 0 if entries else len(failures),
            "write": counts["write"],
            "unchanged": counts["unchanged"],
            "platform_totals": dict(quantities),
            "total_formulas_to_write": sum(e["status"] == "write" for e in totals),
        },
        "status": "partial" if entries else "needs_review",
    }


def compact_ranges(operations: list[dict]) -> list[dict]:
    """把 operations 中连续同列单格请求合并为最多100行的块，返回批量范围。"""
    cells = []
    for operation in operations:
        sid, area = operation["range"].split("!", 1)
        match = re.fullmatch(r"([A-Z]+)([0-9]+):\1\2", area)
        if not match:
            raise ValueError("合并请求只接受单格范围")
        col, row = match.groups()
        cells.append((sid, col, int(row), operation["values"][0]))
    cells.sort(key=lambda cell: (cell[0], column_number(cell[1]), cell[2]))
    blocks = []
    for sid, col, row, value in cells:
        if (
            blocks
            and blocks[-1]["sid"] == sid
            and blocks[-1]["col"] == col
            and blocks[-1]["last"] + 1 == row
            and len(blocks[-1]["values"]) < 100
        ):
            blocks[-1]["last"] = row
            blocks[-1]["values"].append(value)
        else:
            blocks.append({"sid": sid, "col": col, "first": row, "last": row, "values": [value]})
    return [
        {"range": f"{b['sid']}!{b['col']}{b['first']}:{b['col']}{b['last']}", "values": b["values"]}
        for b in blocks
    ]
