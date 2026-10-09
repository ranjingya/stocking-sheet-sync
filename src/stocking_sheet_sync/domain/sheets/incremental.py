"""按款式与平台补齐空白，隔离旧数据冲突。"""

import logging
from collections import Counter, defaultdict

from stocking_sheet_sync.domain.sheets.platforms import compact_ranges
from stocking_sheet_sync.domain.sheets.totals import SUM_RANGE

LOG = logging.getLogger(__name__)


def filled(cell):
    """判断cell是否已有内容，数字0和公式均视为已填。"""
    return bool(cell.get("formula")) or cell.get("value") not in (None, "")


def fill_state(snapshot, rows, columns, *, forecast, overwrite):
    """
    功能说明：按款式平台确定补充状态。

    参数：snapshot：表格快照；rows：同款全部商品行；columns：指标列映射；
        forecast：是否请求预测；overwrite：是否强制重填。
    返回值：首次填充、补历史、补预估、已完整跳过或强制重填。
    """
    if overwrite:
        return "强制重填"
    history = ["sales", "previous", "future"] if forecast else ["sales"]
    present = [
        filled(snapshot["cells"].get(f"{columns.get(m, '')}{r['row']}", {}))
        for r in rows
        for m in history
    ]
    predictions = (
        [
            filled(snapshot["cells"].get(f"{columns.get('forecast', '')}{r['row']}", {}))
            for r in rows
        ]
        if forecast
        else []
    )
    if all(present) and all(predictions):
        return "已完整跳过"
    if not any(present + predictions):
        return "首次填充"
    return "补预估" if all(present) else "补历史并检查预估" if forecast else "补历史"


def isolate_scopes(update, snapshot, *, blocked=None, metric_scoped=False):
    """
    功能说明：隔离同款同平台历史冲突，保留其他款的写入，并核对整列合计。

    参数：update：未经平台隔离的写入计划；snapshot：当前表格快照；
        blocked：已识别的(款号,平台)历史问题。
        metric_scoped：是否进一步按历史指标隔离，供自动填充使用。
    返回值：按款平台隔离后的写入计划，包含逐款阻断原因。
    """
    if update.get("target_issues"):
        return update
    failures = {key: list(value) for key, value in (blocked or {}).items()}
    forecast_failures = set()

    def scope_key(entry):
        key = (entry["style"], entry["platform"].split(":")[0])
        return (*key, entry["platform"].split(":")[-1]) if metric_scoped else key

    for entry in update["entries"]:
        if entry["status"] == "needs_review":
            key = scope_key(entry)
            failures.setdefault(key, []).extend(entry.get("issues") or ["数据不完整"])
    if metric_scoped:
        for entry in update["entries"]:
            if "target_conflict" not in entry.get("issues", []):
                continue
            pair = scope_key(entry)[:2]
            reason = (
                f"历史数据不一致（{entry['target_cell']}：表内{entry.get('existing_quantity')}，"
                f"来源{entry.get('quantity')}）"
            )
            for other in update["entries"]:
                if scope_key(other)[:2] == pair:
                    failures.setdefault(scope_key(other), []).append(reason)
    for total in update.get("total_entries", []):
        if total["status"] != "needs_review":
            continue
        pid = total["platform"].split(":")[0]
        if total["platform"].endswith(":forecast"):
            forecast_failures.add(pid)
        else:
            for entry in update["entries"]:
                if (
                    entry["platform"] == total["platform"]
                    if metric_scoped
                    else entry["platform"].split(":")[0] == pid
                ):
                    failures.setdefault(scope_key(entry), []).append("历史合计已有不同内容")

    def active(entry):
        key = scope_key(entry)
        if (
            metric_scoped
            and entry["platform"].endswith(":forecast")
            and any(failed[:2] == key[:2] for failed in failures)
        ):
            return False
        return key not in failures and not (
            entry["platform"].endswith(":forecast")
            and entry["platform"].split(":")[0] in forecast_failures
        )

    entries = [e for e in update["entries"] if active(e)]
    quantities = defaultdict(int)
    operations = []
    for entry in entries:
        quantities[entry["platform"]] += entry["quantity"]
        if entry["status"] == "write":
            address = entry["target_cell"]
            operations.append(
                {
                    "range": f"{snapshot['sheet_id']}!{address}:{address}",
                    "values": [[entry["quantity"]]],
                }
            )
    targets = {e["target_cell"]: e["quantity"] for e in entries}
    totals = []
    for total in update.get("total_entries", []):
        if total["status"] == "needs_review" or not any(
            e["platform"] == total["platform"] for e in entries
        ):
            continue
        match = SUM_RANGE.fullmatch(total["formula"].replace(" ", ""))
        if not match:
            # 原有复杂公式交给表格计算，通用回读器验证公式文本保持不变。
            continue
        col, first, last = match.groups()
        values = [
            targets.get(f"{col}{r}", snapshot["cells"].get(f"{col}{r}", {}).get("value"))
            for r in range(int(first), int(last) + 1)
        ]
        total = {**total, "expected_quantity": sum(v for v in values if type(v) in (int, float))}
        totals.append(total)
        if total["status"] == "write":
            address = total["target_cell"]
            operations.append(
                {
                    "range": f"{snapshot['sheet_id']}!{address}:{address}",
                    "values": [[{"type": "formula", "text": total["formula"]}]],
                }
            )
    if metric_scoped and not entries:
        operations, totals = [], []
    counts = Counter(e["status"] for e in entries)
    return {
        **update,
        "entries": entries,
        "total_entries": totals,
        "skipped_entries": [e for e in update["entries"] if not active(e)],
        "blocked_scopes": [
            {
                "style": key[0],
                "platform": key[1],
                "issues": sorted(set(issues)),
                **({"metric": key[2]} if metric_scoped else {}),
            }
            for key, issues in failures.items()
        ],
        "blocked_forecasts": {pid: ["预测合计已有不同内容"] for pid in forecast_failures},
        "operations": compact_ranges(operations),
        "summary": {
            **update["summary"],
            "needs_review": len(failures) + len(forecast_failures)
            if metric_scoped and not entries
            else 0,
            "write": counts["write"],
            "unchanged": counts["unchanged"],
            "platform_totals": dict(quantities),
        },
        "status": "partial"
        if failures or forecast_failures
        else "changes_proposed"
        if operations
        else "unchanged",
    }
