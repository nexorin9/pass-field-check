"""Audit log: append JSONL events + monthly CSV export.

设计要点
--------
- ``append_audit(event, audit_path)`` 在 ``event`` 上补 ISO 8601 (UTC) ``timestamp``
  后,以 ``ensure_ascii=False`` 写一行 JSONL。父目录自动创建。同一进程可重跑
  (幂等追加,不修改历史行)。
- ``export_month(month, audit_path, out_csv)`` 读 audit.jsonl,按
  ``timestamp.startswith(month)`` 过滤当月事件,写出 utf-8-sig BOM CSV
  (Excel 直开不乱码);幂等 (重跑只覆盖 out_csv,不会向 audit.jsonl 重复追加)。
- 列顺序固定为 ``timestamp / rule_id / order_hash / session_id / operator /
  action / confirmed``,便于药事办 Excel 透视。
- 字段对齐 contract.py ``SCHEMA_HIS_REVIEW_FIELD`` 与 tools.apply_rule
  返回字段 (rule_id / order_hash / session_id / operator / confirmed),便于
  REST / CLI / audit 三处共用一份 schema。

参考
----
本模块为新写实现,与 build-hermes-plugin.py 的 ``agents`` 注册形态无直接
关系,但 ``ensure_ascii=False`` + atomic append 与 scripts/rx-index-build.py
的 atomic write (tmp + rename) 一致:不静默吞错,失败由 caller raise。
"""

from __future__ import annotations

import csv
import datetime as _dt
import gzip
import json
import os
from pathlib import Path
from typing import Any

# CSV 列顺序(固定,便于 Excel 透视与回归断言)
CSV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "rule_id",
    "order_hash",
    "session_id",
    "operator",
    "action",
    "confirmed",
)

# 合法的 action 白名单,便于 export_month 校验
VALID_ACTIONS: frozenset[str] = frozenset({"search", "inspect", "load", "apply"})


def _ensure_timestamp(event: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``event`` with an ISO 8601 UTC timestamp.

    若调用方已传入 ``timestamp`` (e.g. 来自 apply_rule 的 ``applied_at``),保留
    原值;否则补 ``datetime.now(timezone.utc).isoformat()``。
    """
    record = dict(event)
    record.setdefault(
        "timestamp", _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    return record


def append_audit(event: dict[str, Any], audit_path: Path) -> dict[str, Any]:
    """Append one audit row (JSONL) to ``audit_path``.

    自动写入 ``timestamp`` (ISO 8601, UTC);父目录自动创建。

    Parameters
    ----------
    event:
        事件字典。必含或可由 caller 补充的字段:
        rule_id / order_hash / session_id / operator / action
        (∈ {search, inspect, load, apply}) / scores_top_n (list[float]) /
        confirmed (bool)。
    audit_path:
        目标 JSONL 文件路径。父目录不存在会自动 ``mkdir -p``。

    Returns
    -------
    dict
        写入磁盘的最终 record(含 ``timestamp``),便于 caller 进一步处理
        (e.g. 调试时打印)。
    """
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _ensure_timestamp(event)
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
    return record


def _iter_audit_records(audit_path: Path) -> list[dict[str, Any]]:
    """Read all audit rows from ``audit_path`` as a list of dicts.

    空行 / 非法 JSON 行静默跳过 (与 cli.py ``_read_audit_events`` 一致),
    但 audit_path 不存在时 raise (caller 应显式处理)。
    """
    path = Path(audit_path)
    if not path.exists():
        raise FileNotFoundError(f"audit log not found: {path}")
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _month_prefix(month: str) -> str:
    """Validate ``month`` is 'YYYY-MM' and return the literal prefix string."""
    if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
        raise ValueError(f"month 必须是 'YYYY-MM' 格式: {month!r}")
    year_part, mon_part = month[:4], month[5:]
    if not (year_part.isdigit() and mon_part.isdigit()):
        raise ValueError(f"month 必须是 'YYYY-MM' 格式: {month!r}")
    mon_int = int(mon_part)
    if not 1 <= mon_int <= 12:
        raise ValueError(f"month 月份非法: {month!r}")
    return month


def _format_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    """Project an audit record to the fixed CSV column set.

    缺字段填 ``""``(便于 Excel 直开空单元格);list / dict 字段
    (如 ``scores_top_n``) 不进入 CSV (CSV 形态约束),仅在 JSONL 中保留。
    """
    row: dict[str, str] = {col: "" for col in CSV_COLUMNS}
    for col in CSV_COLUMNS:
        val = record.get(col, "")
        if val is None:
            row[col] = ""
        elif isinstance(val, bool):
            # 写 'True' / 'False'(Excel 直读)
            row[col] = "True" if val else "False"
        else:
            row[col] = str(val)
    return row


def export_month(
    month: str,
    audit_path: Path,
    out_csv: Path,
    *,
    overwrite: bool = True,
) -> int:
    """Export audit rows of ``month`` (YYYY-MM) from ``audit_path`` to ``out_csv``.

    Parameters
    ----------
    month:
        'YYYY-MM' 格式的月份过滤串。
    audit_path:
        输入 audit.jsonl 路径;不存在 raise ``FileNotFoundError``。
    out_csv:
        输出 CSV 路径;父目录自动创建。默认覆盖 (``overwrite=True``);
        若 ``overwrite=False`` 且文件已存在则 raise ``FileExistsError``,
        便于 caller 防止误覆盖。
    Returns
    -------
    int
        写入 CSV 的数据行数(不含表头)。
    """
    prefix = _month_prefix(month)
    path = Path(audit_path)
    out_path = Path(out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and out_path.exists():
        raise FileExistsError(f"out_csv 已存在,拒绝覆盖: {out_path}")

    records = _iter_audit_records(path)
    rows = [
        _format_csv_row(r)
        for r in records
        if isinstance(r.get("timestamp"), str)
        and r["timestamp"].startswith(prefix)
    ]

    # utf-8-sig BOM 让 Excel 直开不乱码
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


__all__ = [
    "CSV_COLUMNS",
    "VALID_ACTIONS",
    "append_audit",
    "archive_month",
    "export_month",
    "monthly_summary",
    "query_events",
    "rotate_if_oversize",
]


# ---------------------------------------------------------------------------
# query_events — 检索 / 过滤 / 分页(供 REST GET /audit/events 调用)
# ---------------------------------------------------------------------------


def _parse_iso8601(value: Any) -> _dt.datetime | None:
    """Best-effort parse of an ISO 8601 timestamp; returns None on failure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _matches_filter(
    record: dict[str, Any],
    *,
    operator: str | None,
    rule_id: str | None,
    action: str | None,
    start: _dt.datetime | None,
    end: _dt.datetime | None,
) -> bool:
    """Single-record predicate used by :func:`query_events`.

    空过滤值视为不约束该字段;字符串过滤做严格相等(便于审计追溯)。
    时间过滤使用 ``[start, end)`` 半开区间(避免边界事件双重计入)。
    """
    if operator is not None and str(record.get("operator", "")) != operator:
        return False
    if rule_id is not None and str(record.get("rule_id", "")) != rule_id:
        return False
    if action is not None and str(record.get("action", "")) != action:
        return False
    if start is not None or end is not None:
        ts = _parse_iso8601(record.get("timestamp"))
        if ts is None:
            return False
        if start is not None and ts < start:
            return False
        if end is not None and ts >= end:
            return False
    return True


def query_events(
    audit_path: Path,
    *,
    operator: str | None = None,
    rule_id: str | None = None,
    start: str | _dt.datetime | None = None,
    end: str | _dt.datetime | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Filter + paginate audit rows from ``audit_path``.

    Parameters
    ----------
    audit_path:
        JSONL 文件路径。不存在时返回 ``{events: [], total: 0, ...}``
        (不抛错,便于 REST 端 200 + 空列表)。
    operator / rule_id / action:
        严格相等过滤;``None`` 或空串视为不约束。
    start / end:
        ISO 8601 字符串或 ``datetime``;半开区间 ``[start, end)``。
    page / page_size:
        1-based 分页;``page_size`` 自动 clamp 到 [1, 500],``page`` clamp 到 >=1。
        返回 dict 含 ``events / total / page / page_size`` 四键。

    Returns
    -------
    dict
        ``{events: list[dict], total: int, page: int, page_size: int}``
        ``events`` 已按 ``timestamp`` 升序排列(便于分页稳定)。
    """
    path = Path(audit_path)
    if not path.exists():
        # 不存在 → 空结果(便于 REST 端 200 + 空列表;与 export_month 不同)
        return {
            "events": [],
            "total": 0,
            "page": max(1, int(page)),
            "page_size": max(1, min(int(page_size), 500)),
        }

    # 时间过滤做类型归一:字符串 → datetime(便于比较)
    start_dt = (
        _parse_iso8601(start)
        if isinstance(start, str)
        else (start if isinstance(start, _dt.datetime) else None)
    )
    end_dt = (
        _parse_iso8601(end)
        if isinstance(end, str)
        else (end if isinstance(end, _dt.datetime) else None)
    )

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    records = _iter_audit_records(path)
    matched: list[dict[str, Any]] = [
        r
        for r in records
        if _matches_filter(
            r,
            operator=operator or None,
            rule_id=rule_id or None,
            action=action or None,
            start=start_dt,
            end=end_dt,
        )
    ]
    # 稳定排序:timestamp 升序 → 无 timestamp 的排最后(rule_id 字典序保稳定)
    matched.sort(
        key=lambda r: (
            str(r.get("timestamp", "")),
            str(r.get("rule_id", "")),
        )
    )

    total = len(matched)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_events = matched[start_idx:end_idx]
    return {
        "events": page_events,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# archive_month / rotate_if_oversize — JSONL 月度归档 + 体积守门
# ---------------------------------------------------------------------------


def _atomic_replace(src: Path, dst: Path) -> None:
    """Replace ``dst`` with ``src`` (rename on POSIX; fallback copy on error).

    用于 archive_month 把过滤后的 audit.jsonl 原子地写回原位,避免在写盘途中
    进程被杀留下空文件导致数据丢失。
    """
    src.replace(dst)


def archive_month(
    month: str,
    audit_path: Path,
    archive_dir: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Archive a single month's events from ``audit_path`` to a gzipped JSONL.

    Parameters
    ----------
    month:
        ``YYYY-MM`` 格式的月份过滤串。
    audit_path:
        源 audit.jsonl 路径;不存在或当月无事件时,返回 ``archived_count=0``
        且不在磁盘上写任何东西(便于 cron 静默调用)。
    archive_dir:
        归档目录(不存在自动 ``mkdir -p``);目标文件
        ``archive_dir / f"audit-{month}.jsonl.gz"``。append-only:
        同月再次归档默认拒绝(``overwrite=False``),由 caller 显式确认覆盖。
    overwrite:
        True 时覆盖已存在的归档;默认 False → 已存在则 raise ``FileExistsError``。

    Returns
    -------
    dict
        ``{month, archive_path, archived_count, remaining_count}`` 四键,
        便于 CLI / REST 输出 + audit log 二次写入。

    Notes
    -----
    - 归档后 ``audit.jsonl`` 仅保留**非当月**事件(原子地 tmp + rename)。
    - 归档文件使用 ``gzip`` 压缩 + JSONL 一行一条 + ``ensure_ascii=False``;
      与 audit.jsonl 主文件格式一致,便于 ``gunzip | jq`` 直接读。
    - 空月份返回 ``archived_count=0`` 且不创建归档文件,避免空 .gz 占用磁盘。
    """
    prefix = _month_prefix(month)
    audit_path = Path(audit_path)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"audit-{prefix}.jsonl.gz"

    if not audit_path.exists():
        return {
            "month": prefix,
            "archive_path": str(archive_path),
            "archived_count": 0,
            "remaining_count": 0,
        }

    records = _iter_audit_records(audit_path)
    in_month: list[dict[str, Any]] = []
    out_of_month: list[dict[str, Any]] = []
    for rec in records:
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts.startswith(prefix):
            in_month.append(rec)
        else:
            out_of_month.append(rec)

    if not in_month:
        return {
            "month": prefix,
            "archive_path": str(archive_path),
            "archived_count": 0,
            "remaining_count": len(records),
        }

    if archive_path.exists() and not overwrite:
        raise FileExistsError(
            f"archive 已存在,拒绝覆盖: {archive_path} (overwrite=True 可强制覆盖)"
        )

    # 1) 写归档文件:gzip 压缩 + JSONL 一行一条 + ensure_ascii=False
    tmp_archive = archive_path.with_suffix(archive_path.suffix + ".tmp")
    with gzip.open(tmp_archive, "wt", encoding="utf-8") as fh:
        for rec in in_month:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
    tmp_archive.replace(archive_path)

    # 2) 重写 audit.jsonl:仅保留 out_of_month(原子 tmp + rename)
    tmp_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
    with tmp_audit.open("w", encoding="utf-8") as fh:
        for rec in out_of_month:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
    _atomic_replace(tmp_audit, audit_path)

    return {
        "month": prefix,
        "archive_path": str(archive_path),
        "archived_count": len(in_month),
        "remaining_count": len(out_of_month),
    }


def list_archived_months(audit_path: Path) -> list[str]:
    """列出 ``audit_path`` 中历史出现的所有月份(YYYY-MM,升序去重)。

    用于 ``rotate_if_oversize`` 取「最老一月」以及 CLI 调试。无 audit_path
    或文件不存在返回空列表(不 raise,便于轮询脚本默认行为)。
    """
    if not audit_path.exists():
        return []
    months: set[str] = set()
    for rec in _iter_audit_records(audit_path):
        ts = rec.get("timestamp")
        if isinstance(ts, str) and len(ts) >= 7 and ts[4] == "-":
            months.add(ts[:7])
    return sorted(months)


def rotate_if_oversize(
    audit_path: Path,
    archive_dir: Path,
    *,
    max_mb: float = 50.0,
) -> dict[str, Any]:
    """Check ``audit_path`` size and archive the oldest month if over ``max_mb``.

    Parameters
    ----------
    audit_path:
        待守门的 audit.jsonl;不存在 / 0 字节 → 视为无需 rotate。
    archive_dir:
        归档输出目录(传给 :func:`archive_month`)。
    max_mb:
        体积上限(MB),默认 50。低于上限 → 返回 ``rotated=False``。

    Returns
    -------
    dict
        ``{rotated: bool, size_mb: float, max_mb: float, month: str | None,
        archive_path: str | None, archived_count: int}`` 便于 caller 决定
        是否记入操作日志。

    Notes
    -----
    仅归档「最老一月」(按 timestamp 升序第一月)。若该月归档已存在,默认
    ``overwrite=False`` 走安全路径 → 返回 ``rotated=False`` 并附
    ``error="archive_exists"``,由 caller 决定是否升级 ``overwrite=True``。
    """
    path = Path(audit_path)
    if not path.exists():
        return {
            "rotated": False,
            "size_mb": 0.0,
            "max_mb": float(max_mb),
            "month": None,
            "archive_path": None,
            "archived_count": 0,
        }
    size_bytes = os.path.getsize(path)
    size_mb = size_bytes / (1024 * 1024)
    if size_mb <= float(max_mb):
        return {
            "rotated": False,
            "size_mb": round(size_mb, 4),
            "max_mb": float(max_mb),
            "month": None,
            "archive_path": None,
            "archived_count": 0,
        }

    months = list_archived_months(path)
    if not months:
        return {
            "rotated": False,
            "size_mb": round(size_mb, 4),
            "max_mb": float(max_mb),
            "month": None,
            "archive_path": None,
            "archived_count": 0,
        }
    oldest = months[0]
    archive_path = Path(archive_dir) / f"audit-{oldest}.jsonl.gz"
    try:
        result = archive_month(oldest, path, archive_dir, overwrite=False)
    except FileExistsError:
        return {
            "rotated": False,
            "size_mb": round(size_mb, 4),
            "max_mb": float(max_mb),
            "month": oldest,
            "archive_path": str(archive_path),
            "archived_count": 0,
            "error": "archive_exists",
        }
    return {
        "rotated": True,
        "size_mb": round(size_mb, 4),
        "max_mb": float(max_mb),
        "month": oldest,
        "archive_path": result["archive_path"],
        "archived_count": result["archived_count"],
    }


# ---------------------------------------------------------------------------
# monthly_summary / _format_summary_markdown — 药事管理月度简报聚合
# ---------------------------------------------------------------------------
#
# 药事办每月要向药事管理与药物治疗学委员会交一页「本月审方情况」:哪几条
# 规则命中最多(说明该类医嘱风险集中)、哪些药师在用、严重度分布如何、有多少
# apply 已被人工确认。本节把 audit.jsonl 聚合成这一页,CLI `audit-summary`
# 直接输出 markdown,可粘进简报或钉进科务会材料。
#
# 边界:confirmed=False 是本产品的默认返回值(永不代签),因此 confirmation_rate
# 反映的是「药师回写确认」的比例,而不是规则命中的正确率。


def _load_rule_severity_map(rules_path: Path | None) -> dict[str, str]:
    """Read ``data/rules.json`` and return ``{rule_id: severity}``.

    ``rules_path`` 为 None / 文件不存在 / JSON 结构异常时返回空 dict —— 严重度
    分布随之退化为 ``unknown``,不阻塞简报生成(药事办常在未 build 索引的机器上
    直接读 audit 归档)。
    """
    if rules_path is None:
        return {}
    path = Path(rules_path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return {}
    out: dict[str, str] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("rule_id")
        severity = rule.get("severity")
        if isinstance(rule_id, str) and isinstance(severity, str):
            out[rule_id] = severity
    return out


def _count_desc(counter: dict[str, int]) -> list[dict[str, Any]]:
    """Turn a ``{key: count}`` map into a count-desc / key-asc ordered list.

    返回 ``[{"key": ..., "count": ...}]``;同计数时按 key 字典序,保证简报在
    同一份 audit 上可重复生成完全相同的文本(便于 diff 与留档)。
    """
    return [
        {"key": key, "count": count}
        for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# 严重度展示顺序(与 runtime.SEVERITY_RANK 一致;unknown 排最后)
_SEVERITY_ORDER: tuple[str, ...] = ("high", "medium", "low", "unknown")


def monthly_summary(
    month: str,
    audit_path: Path,
    rules_path: Path | None = None,
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Aggregate one month of audit events into a 药事管理简报 payload.

    Parameters
    ----------
    month:
        ``YYYY-MM`` 月份过滤串(非法格式 raise ``ValueError``)。
    audit_path:
        audit.jsonl 路径。不存在时返回全零简报(``total_events=0``),不 raise
        —— 新上线的院区当月可能还没有任何审方留痕。
    rules_path:
        可选 ``data/rules.json`` 路径,用于 join 每条 rule 的 severity。缺省或
        不可读时,severity 归入 ``unknown``。
    top_n:
        ``top_triggered_rules`` 截断条数(默认 10)。

    Returns
    -------
    dict
        ``{month, total_events, action_distribution, top_triggered_rules,
        operator_distribution, severity_distribution, apply_count,
        confirmed_count, unconfirmed_count, confirmation_rate,
        daily_activity, distinct_rules, distinct_operators,
        severity_joined}``

        - ``top_triggered_rules``: ``[{rule_id, severity, count}]`` 计数倒序,
          最多 ``top_n`` 条。
        - ``operator_distribution`` / ``severity_distribution``:
          ``[{key, count}]`` 计数倒序(severity 按 high→medium→low→unknown 固定序)。
        - ``confirmation_rate``: ``confirmed_count / apply_count``,
          ``apply_count == 0`` 时为 ``0.0``;取值恒在 ``[0.0, 1.0]``。
        - ``daily_activity``: ``[{date, count}]`` 按日期升序,只列当月有事件的天。
    """
    prefix = _month_prefix(month)
    path = Path(audit_path)

    records: list[dict[str, Any]] = []
    if path.exists():
        records = [
            rec
            for rec in _iter_audit_records(path)
            if isinstance(rec.get("timestamp"), str)
            and rec["timestamp"].startswith(prefix)
        ]

    severity_map = _load_rule_severity_map(rules_path)

    rule_counter: dict[str, int] = {}
    operator_counter: dict[str, int] = {}
    action_counter: dict[str, int] = {}
    severity_counter: dict[str, int] = {}
    daily_counter: dict[str, int] = {}
    apply_count = 0
    confirmed_count = 0

    for rec in records:
        rule_id = str(rec.get("rule_id", "") or "(none)")
        rule_counter[rule_id] = rule_counter.get(rule_id, 0) + 1

        operator = str(rec.get("operator", "") or "(none)")
        operator_counter[operator] = operator_counter.get(operator, 0) + 1

        action = str(rec.get("action", "") or "(none)")
        action_counter[action] = action_counter.get(action, 0) + 1

        severity = severity_map.get(rule_id, "unknown")
        severity_counter[severity] = severity_counter.get(severity, 0) + 1

        # timestamp 已确认为 'YYYY-MM...' 前缀,前 10 位即 ISO 日期
        day = str(rec["timestamp"])[:10]
        daily_counter[day] = daily_counter.get(day, 0) + 1

        if action == "apply":
            apply_count += 1
            if bool(rec.get("confirmed", False)):
                confirmed_count += 1

    top_rules = [
        {
            "rule_id": item["key"],
            "severity": severity_map.get(item["key"], "unknown"),
            "count": item["count"],
        }
        for item in _count_desc(rule_counter)[: max(1, int(top_n))]
    ]

    # severity 用固定业务序(high 最前),而非计数序 —— 简报读者先看 high。
    severity_distribution = [
        {"key": sev, "count": severity_counter[sev]}
        for sev in _SEVERITY_ORDER
        if sev in severity_counter
    ]
    # 兜底:rules.json 里出现了 _SEVERITY_ORDER 之外的值也不丢
    severity_distribution.extend(
        item
        for item in _count_desc(severity_counter)
        if item["key"] not in _SEVERITY_ORDER
    )

    confirmation_rate = (
        round(confirmed_count / apply_count, 4) if apply_count else 0.0
    )

    return {
        "month": prefix,
        "total_events": len(records),
        "action_distribution": _count_desc(action_counter),
        "top_triggered_rules": top_rules,
        "operator_distribution": _count_desc(operator_counter),
        "severity_distribution": severity_distribution,
        "apply_count": apply_count,
        "confirmed_count": confirmed_count,
        "unconfirmed_count": apply_count - confirmed_count,
        "confirmation_rate": confirmation_rate,
        "daily_activity": [
            {"date": day, "count": daily_counter[day]}
            for day in sorted(daily_counter)
        ],
        "distinct_rules": len(rule_counter),
        "distinct_operators": len(operator_counter),
        "severity_joined": bool(severity_map),
    }


def _pct(count: int, total: int) -> str:
    """Format ``count/total`` as a one-decimal percentage string."""
    if total <= 0:
        return "0.0%"
    return f"{count / total * 100:.1f}%"


def _ascii_bar(count: int, peak: int, *, width: int = 24) -> str:
    """Render a proportional ASCII bar; at least one block for non-zero counts."""
    if peak <= 0 or count <= 0:
        return ""
    blocks = max(1, round(count / peak * width))
    return "█" * blocks


def _format_summary_markdown(summary: dict[str, Any]) -> str:
    """Render :func:`monthly_summary` output as a markdown 药事管理简报.

    输出结构(供药事办直接粘贴进月度材料):
      标题 → 概览要点 → 高频命中规则表 → 操作者分布表 → 严重度分布表
      → 每日活跃度表(含 ASCII 柱状)→ 口径说明。

    空月份仍输出完整骨架 + ``本月无审方留痕`` 提示,避免简报出现空白章节。
    """
    month = summary.get("month", "")
    total = int(summary.get("total_events", 0))
    lines: list[str] = []

    lines.append(f"# 用药字段对照工作台 · 月度审方简报({month})")
    lines.append("")

    if total == 0:
        lines.append(f"> 本月({month})无审方留痕,audit.jsonl 中没有匹配事件。")
        lines.append("")
        lines.append("## 口径说明")
        lines.append("")
        lines.append("- 统计范围:audit.jsonl 中 timestamp 落在本月的全部事件。")
        lines.append("")
        return "\n".join(lines)

    apply_count = int(summary.get("apply_count", 0))
    confirmed_count = int(summary.get("confirmed_count", 0))
    rate = float(summary.get("confirmation_rate", 0.0))

    lines.append("## 概览")
    lines.append("")
    lines.append(f"- 事件总数:**{total}** 条")
    lines.append(
        f"- 生成审方意见(apply):**{apply_count}** 条;"
        f"药师已回写确认 **{confirmed_count}** 条,确认率 **{rate * 100:.1f}%**"
    )
    lines.append(
        f"- 涉及规则 **{summary.get('distinct_rules', 0)}** 条,"
        f"参与人员 **{summary.get('distinct_operators', 0)}** 人"
    )
    lines.append("")

    lines.append("## 高频命中规则")
    lines.append("")
    lines.append("| 排名 | 规则 | 严重度 | 命中次数 | 占比 |")
    lines.append("|------|------|--------|----------|------|")
    for idx, item in enumerate(summary.get("top_triggered_rules", []), start=1):
        lines.append(
            f"| {idx} | {item.get('rule_id', '')} | {item.get('severity', 'unknown')} "
            f"| {item.get('count', 0)} | {_pct(int(item.get('count', 0)), total)} |"
        )
    lines.append("")

    lines.append("## 操作者分布")
    lines.append("")
    lines.append("| 操作者 | 事件数 | 占比 |")
    lines.append("|--------|--------|------|")
    for item in summary.get("operator_distribution", []):
        lines.append(
            f"| {item.get('key', '')} | {item.get('count', 0)} "
            f"| {_pct(int(item.get('count', 0)), total)} |"
        )
    lines.append("")

    lines.append("## 严重度分布")
    lines.append("")
    lines.append("| 严重度 | 事件数 | 占比 |")
    lines.append("|--------|--------|------|")
    for item in summary.get("severity_distribution", []):
        lines.append(
            f"| {item.get('key', '')} | {item.get('count', 0)} "
            f"| {_pct(int(item.get('count', 0)), total)} |"
        )
    lines.append("")

    daily = summary.get("daily_activity", [])
    peak = max((int(d.get("count", 0)) for d in daily), default=0)
    lines.append("## 每日活跃度")
    lines.append("")
    lines.append("| 日期 | 事件数 | 分布 |")
    lines.append("|------|--------|------|")
    for item in daily:
        count = int(item.get("count", 0))
        lines.append(
            f"| {item.get('date', '')} | {count} | {_ascii_bar(count, peak)} |"
        )
    lines.append("")

    lines.append("## 口径说明")
    lines.append("")
    lines.append("- 统计范围:audit.jsonl 中 timestamp 落在本月的全部事件。")
    lines.append(
        "- 确认率 = action=apply 且 confirmed=true 的条数 ÷ apply 总条数。"
        "本工具生成的审方意见默认 confirmed=false(永不代签),"
        "须由审方药师在 HIS 审方栏人工确认后回写。"
    )
    if not summary.get("severity_joined", False):
        lines.append(
            "- 严重度显示为 unknown:未提供 data/rules.json,"
            "请先执行 `pass-fc index-build` 后重新出简报。"
        )
    lines.append("")

    return "\n".join(lines)