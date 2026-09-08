"""Tests for pass_field_check.audit (append_audit + export_month).

覆盖 append + 跨月过滤 + CSV BOM + 列顺序 + 幂等覆盖 + 列缺字段。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path

import pytest

from pass_field_check.audit import (
    CSV_COLUMNS,
    VALID_ACTIONS,
    append_audit,
    export_month,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    rule_id: str = "rx-aminoglycoside-pediatric",
    order_hash: str | None = None,
    session_id: str | None = None,
    operator: str = "pharmacist-001",
    action: str = "apply",
    scores_top_n: list[float] | None = None,
    confirmed: bool = False,
    timestamp: str | None = None,
    extra: dict | None = None,
) -> dict:
    """构造一条审计事件,字段对齐 apply_rule 返回 payload。"""
    event: dict = {
        "rule_id": rule_id,
        "order_hash": order_hash or "a" * 64,
        "session_id": session_id or "b" * 16,
        "operator": operator,
        "action": action,
        "scores_top_n": scores_top_n if scores_top_n is not None else [0.9, 0.7],
        "confirmed": confirmed,
    }
    if timestamp is not None:
        event["timestamp"] = timestamp
    if extra:
        event.update(extra)
    return event


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# append_audit
# ---------------------------------------------------------------------------


class TestAppendAudit:
    def test_append_audit_writes_one_jsonl_line(self, tmp_path: Path):
        path = tmp_path / "audit" / "audit.jsonl"
        record = append_audit(_make_event(), path)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        # 写回的 record 含 timestamp
        assert "timestamp" in record
        # 写回磁盘的内容 = jsonl 解析结果 = record
        assert json.loads(lines[0]) == record

    def test_append_audit_preserves_caller_timestamp(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        fixed_ts = "2026-09-08T10:00:00+00:00"
        append_audit(_make_event(timestamp=fixed_ts), path)
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert record["timestamp"] == fixed_ts

    def test_append_audit_adds_iso8601_utc_when_missing(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        record = append_audit(_make_event(), path)
        # 解析后能拿到 datetime (ISO 8601)
        parsed = _dt.datetime.fromisoformat(record["timestamp"])
        assert parsed.tzinfo is not None

    def test_append_audit_appends_multiple_lines(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        for i in range(3):
            append_audit(_make_event(operator=f"pharmacist-{i:03d}"), path)
        rows = _read_jsonl(path)
        assert len(rows) == 3
        assert [r["operator"] for r in rows] == [
            "pharmacist-000",
            "pharmacist-001",
            "pharmacist-002",
        ]

    def test_append_audit_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "nested" / "audit.jsonl"
        assert not path.parent.exists()
        append_audit(_make_event(), path)
        assert path.exists()

    def test_append_audit_ensure_ascii_false_chinese_safe(self, tmp_path: Path):
        """中文 operator / evidence_excerpt 写入不会被转义为 \\uXXXX。"""
        path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(operator="药师甲", extra={"evidence_excerpt": "儿童应避免"}),
            path,
        )
        raw = path.read_text(encoding="utf-8")
        assert "药师甲" in raw
        assert "儿童应避免" in raw
        assert "\\u" not in raw


# ---------------------------------------------------------------------------
# export_month — 跨月过滤
# ---------------------------------------------------------------------------


class TestExportMonth:
    def _seed_two_months(self, tmp_path: Path) -> Path:
        """5 条当月 + 2 条上月 + 1 条下月 → audit.jsonl"""
        path = tmp_path / "audit.jsonl"
        aug = "2026-08-15T08:00:00+00:00"
        sep = "2026-09-08T08:00:00+00:00"
        octo = "2026-10-01T08:00:00+00:00"
        # 8 月 2 条
        append_audit(_make_event(timestamp=aug, operator="aug-1"), path)
        append_audit(_make_event(timestamp=aug, operator="aug-2"), path)
        # 9 月 5 条
        for i in range(5):
            append_audit(
                _make_event(
                    timestamp=sep, operator=f"sep-{i}", action="apply"
                ),
                path,
            )
        # 10 月 1 条
        append_audit(_make_event(timestamp=octo, operator="oct-1"), path)
        return path

    def test_export_month_filters_correct_month(self, tmp_path: Path):
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "export.csv"
        n = export_month("2026-09", path, out_csv)
        assert n == 5

        # 内容校验:全部 operator 以 'sep-' 开头
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 5
        assert all(r["operator"].startswith("sep-") for r in rows)

    def test_export_month_csv_has_utf8_sig_bom(self, tmp_path: Path):
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "export.csv"
        export_month("2026-09", path, out_csv)
        # 首字节应为 utf-8-sig BOM
        raw = out_csv.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_export_month_csv_columns_in_fixed_order(self, tmp_path: Path):
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "export.csv"
        export_month("2026-09", path, out_csv)
        # 读 BOM + header
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader)
        assert header == list(CSV_COLUMNS)
        assert header == [
            "timestamp",
            "rule_id",
            "order_hash",
            "session_id",
            "operator",
            "action",
            "confirmed",
        ]

    def test_export_month_csv_columns_match_contract(self, tmp_path: Path):
        """CSV 列与 SCHEMA_HIS_REVIEW_FIELD 公共子集对齐 + 多出 timestamp/action。

        evidence_excerpt / applied_at 是 HIS 审方意见 JSON 的字段(≤200 字长文本 +
        ISO 时间),不适合入 CSV。月度审计主要看「何时 / 谁 / 哪条规则 / 是否人工确认」,
        故 CSV 仅保留这 4 维 + 哈希 / 会话 id 便于去重与对账。

        conflicts / recommended_rule_id(Task 32 互斥检测输出)是结构化 JSON
        提示字段,不适合 CSV 扁平形态,同样排除。
        """
        from pass_field_check import contract

        his_required = set(contract.SCHEMA_HIS_REVIEW_FIELD["required"])
        csv_columns = set(CSV_COLUMNS)
        # CSV ⊇ HIS 中适合 CSV 形态的字段
        csv_suitable_subset = {
            "rule_id",
            "order_hash",
            "session_id",
            "operator",
            "confirmed",
        }
        assert csv_suitable_subset.issubset(csv_columns)
        # HIS 的长文本 / ISO 时间 / 结构化 JSON 字段不入 CSV
        long_text_fields = {"evidence_excerpt", "applied_at"}
        json_structured_fields = {"conflicts", "recommended_rule_id"}
        non_csv_fields = long_text_fields | json_structured_fields
        assert non_csv_fields.isdisjoint(csv_columns)
        # CSV 额外含 timestamp + action(便于排序与分桶)
        assert {"timestamp", "action"}.issubset(csv_columns)
        # 全 HIS 必填 = CSV 子集 ∪ 非 CSV 字段
        assert his_required == csv_suitable_subset | non_csv_fields

    def test_export_month_overwrites_existing_csv(self, tmp_path: Path):
        """默认 overwrite=True,重跑只覆盖不追加。"""
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "export.csv"
        export_month("2026-09", path, out_csv)
        first_size = out_csv.stat().st_size
        export_month("2026-09", path, out_csv)
        second_size = out_csv.stat().st_size
        assert first_size == second_size
        # 行数应仍为 5 + 1 header
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            lines = list(csv.reader(fh))
        assert len(lines) == 6  # header + 5 rows

    def test_export_month_overwrite_false_raises_when_exists(
        self, tmp_path: Path
    ):
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "export.csv"
        export_month("2026-09", path, out_csv)
        with pytest.raises(FileExistsError):
            export_month("2026-09", path, out_csv, overwrite=False)

    def test_export_month_creates_parent_dirs(self, tmp_path: Path):
        path = self._seed_two_months(tmp_path)
        out_csv = tmp_path / "deep" / "nested" / "export.csv"
        assert not out_csv.parent.exists()
        export_month("2026-09", path, out_csv)
        assert out_csv.exists()

    def test_export_month_missing_audit_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            export_month(
                "2026-09",
                tmp_path / "missing.jsonl",
                tmp_path / "export.csv",
            )

    def test_export_month_bad_month_format_raises(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        append_audit(_make_event(), path)
        with pytest.raises(ValueError):
            export_month("2026/09", path, tmp_path / "out.csv")
        with pytest.raises(ValueError):
            export_month("26-09", path, tmp_path / "out.csv")
        with pytest.raises(ValueError):
            export_month("2026-13", path, tmp_path / "out.csv")

    def test_export_month_empty_month_returns_zero(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(timestamp="2026-08-01T00:00:00+00:00"), path
        )
        out_csv = tmp_path / "sep.csv"
        n = export_month("2026-09", path, out_csv)
        assert n == 0
        # CSV 仅有 header
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            rows = list(reader)
        assert header == list(CSV_COLUMNS)
        assert rows == []

    def test_export_month_skips_blank_and_malformed_lines(
        self, tmp_path: Path
    ):
        path = tmp_path / "audit.jsonl"
        # 一行有效 + 一行空 + 一行 malformed
        append_audit(_make_event(operator="ok-1"), path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")  # blank
            fh.write("{not json}\n")  # malformed
            fh.write("\n")  # blank again
        append_audit(_make_event(operator="ok-2"), path)
        out_csv = tmp_path / "out.csv"
        n = export_month("2026-09", path, out_csv)
        assert n == 2

    def test_export_month_confirmed_bool_serialized(self, tmp_path: Path):
        """confirmed True/False 序列化为 'True'/'False'(便于 Excel 透视)。"""
        path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(confirmed=False, operator="f-1"), path
        )
        append_audit(
            _make_event(
                confirmed=True, operator="t-1", timestamp="2026-09-08T10:00:00+00:00"
            ),
            path
        )
        out_csv = tmp_path / "out.csv"
        export_month("2026-09", path, out_csv)
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        confirmed_col = sorted(r["confirmed"] for r in rows)
        assert confirmed_col == ["False", "True"]

    def test_export_month_missing_columns_become_empty_string(
        self, tmp_path: Path
    ):
        """记录缺 rule_id 等字段时,CSV 对应列留空(便于 Excel 直开)。"""
        path = tmp_path / "audit.jsonl"
        # 仅含 operator + action;其余字段缺
        append_audit(
            {
                "operator": "minimal",
                "action": "search",
                "timestamp": "2026-09-08T01:00:00+00:00",
            },
            path,
        )
        out_csv = tmp_path / "out.csv"
        export_month("2026-09", path, out_csv)
        with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1
        row = rows[0]
        assert row["operator"] == "minimal"
        assert row["action"] == "search"
        assert row["rule_id"] == ""
        assert row["order_hash"] == ""
        assert row["session_id"] == ""
        assert row["confirmed"] == ""


# ---------------------------------------------------------------------------
# 与 contract / tools.apply_rule 字段对齐 (跨模块契约)
# ---------------------------------------------------------------------------


class TestAuditContractAlignment:
    """保证 audit 写入字段与 apply_rule 返回字段一致,避免 schema drift。"""

    def test_event_fields_align_with_apply_rule_payload(
        self, tmp_path: Path
    ):
        """audit append 的 event 应能容纳 apply_rule 返回的全部 HIS 必填字段。

        apply_rule 返回 SCHEMA_HIS_REVIEW_FIELD 必填子集(Task 32 起扩展为
        9 字段):rule_id / order_hash / session_id / operator / applied_at /
        evidence_excerpt / confirmed / conflicts / recommended_rule_id;
        audit.jsonl 应保留它们(便于审计回查)。
        """
        from pass_field_check.contract import SCHEMA_HIS_REVIEW_FIELD

        his_required = set(SCHEMA_HIS_REVIEW_FIELD["required"])

        # append_audit 接受的 event 应能覆盖 SCHEMA_HIS_REVIEW_FIELD 必填字段
        event = _make_event(
            order_hash="a" * 64,
            session_id="b" * 16,
            confirmed=False,
            extra={
                "applied_at": "2026-09-08T10:00:00+00:00",
                "evidence_excerpt": "儿童应避免使用氨基糖苷类",
                "conflicts": [],
                "recommended_rule_id": None,
            },
        )
        # 必填字段在 event 中
        for key in his_required:
            assert key in event, f"HIS 必填字段 {key} 缺失"

        # 写入 → 读回 → 必填字段保留
        path = tmp_path / "audit.jsonl"
        append_audit(event, path)
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        for key in his_required:
            assert key in record
            assert record[key] == event[key]

    def test_valid_actions_cover_all_tool_actions(self):
        """VALID_ACTIONS 白名单与 4 工具语义对齐 (search/inspect/load/apply)。"""
        # 与 Task 8 register_rx_tools 注册的 4 tool handler action 对齐
        expected = {"search", "inspect", "load", "apply"}
        assert VALID_ACTIONS == expected


# ---------------------------------------------------------------------------
# query_events — 多条件过滤 + 分页(供 REST GET /audit/events 调用)
# ---------------------------------------------------------------------------


class TestQueryEvents:
    def _seed_mixed_audit(self, tmp_path: Path) -> Path:
        """构造跨 3 月 / 4 operator / 2 rule_id / 4 action 的 12 条事件。

        排布:
        - 8 月: 1 条 pharmacist-A01 的 apply / rule-1
        - 9 月: 8 条(4 operator × 2 action,另 2 条 action 跨规则)
        - 10 月: 3 条(2 条 pharmacist-A01 + 1 条 pharmacist-B02)
        共 12 条。便于:
        - 按 operator 过滤
        - 按 rule_id 过滤
        - 按 start_date / end_date 半开区间
        - 按 action 过滤
        - page=2 / page_size=2 分页
        """
        path = tmp_path / "audit.jsonl"
        rows: list[dict] = []
        # 8 月
        rows.append({
            "timestamp": "2026-08-15T08:00:00+00:00",
            "rule_id": "rx-aminoglycoside-pediatric",
            "order_hash": "a" * 64,
            "session_id": "b" * 16,
            "operator": "pharmacist-A01",
            "action": "apply",
            "confirmed": False,
        })
        # 9 月 — pharmacist-A01 × 4
        for i, (action, rid) in enumerate(
            [
                ("search", "rx-aminoglycoside-pediatric"),
                ("inspect", "rx-aminoglycoside-pediatric"),
                ("load", "rx-nsaid-pregnancy"),
                ("apply", "rx-nsaid-pregnancy"),
            ]
        ):
            rows.append({
                "timestamp": f"2026-09-{(i + 1):02d}T0{i + 1}:00:00+00:00",
                "rule_id": rid,
                "order_hash": f"{(i + 1):064x}"[-64:],
                "session_id": f"{i:016x}"[:16],
                "operator": "pharmacist-A01",
                "action": action,
                "confirmed": False,
            })
        # 9 月 — pharmacist-B02 × 2
        for i, (action, rid) in enumerate(
            [
                ("search", "rx-aminoglycoside-pediatric"),
                ("apply", "rx-vancomycin-tdm"),
            ]
        ):
            rows.append({
                "timestamp": f"2026-09-{(i + 5):02d}T0{i + 5}:00:00+00:00",
                "rule_id": rid,
                "order_hash": f"{(i + 5):064x}"[-64:],
                "session_id": f"{i + 5:016x}"[:16],
                "operator": "pharmacist-B02",
                "action": action,
                "confirmed": False,
            })
        # 9 月 — pharmacist-C03 × 2
        for i, (action, rid) in enumerate(
            [
                ("search", "rx-warfarin-inr"),
                ("apply", "rx-acei-pregnancy"),
            ]
        ):
            rows.append({
                "timestamp": f"2026-09-{(i + 7):02d}T0{i + 7}:00:00+00:00",
                "rule_id": rid,
                "order_hash": f"{(i + 7):064x}"[-64:],
                "session_id": f"{i + 7:016x}"[:16],
                "operator": "pharmacist-C03",
                "action": action,
                "confirmed": False,
            })
        # 10 月
        for i, op in enumerate(["pharmacist-A01", "pharmacist-A01", "pharmacist-B02"]):
            rows.append({
                "timestamp": f"2026-10-{(i + 1):02d}T0{i + 1}:00:00+00:00",
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_hash": f"{(i + 9):064x}"[-64:],
                "session_id": f"{i + 9:016x}"[:16],
                "operator": op,
                "action": "search",
                "confirmed": False,
            })
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False))
                fh.write("\n")
        return path

    def test_query_events_no_filter_returns_all_sorted(self, tmp_path: Path):
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(path)
        assert result["total"] == 12
        # events 默认按 timestamp 升序
        timestamps = [ev["timestamp"] for ev in result["events"]]
        assert timestamps == sorted(timestamps)

    def test_query_events_by_operator_filters_correctly(
        self, tmp_path: Path
    ):
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(path, operator="pharmacist-A01")
        # A01: 8 月 1 + 9 月 4 + 10 月 2 = 7
        assert result["total"] == 7
        assert all(ev["operator"] == "pharmacist-A01" for ev in result["events"])

    def test_query_events_by_rule_id_filters_correctly(
        self, tmp_path: Path
    ):
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(
            path, rule_id="rx-aminoglycoside-pediatric"
        )
        # 8 月 1 + 9 月 3(search × 2 + inspect × 1)+ 10 月 3 = 7
        assert result["total"] == 7
        assert all(
            ev["rule_id"] == "rx-aminoglycoside-pediatric"
            for ev in result["events"]
        )

    def test_query_events_by_action_filters_correctly(
        self, tmp_path: Path
    ):
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(path, action="apply")
        # apply 计数:8 月 1 + 9 月 A01×1 + B02×1 + C03×1 + 10 月 0 = 4
        assert result["total"] == 4
        assert all(ev["action"] == "apply" for ev in result["events"])

    def test_query_events_by_date_range_filters_correctly(
        self, tmp_path: Path
    ):
        """跨 9 月与 10 月的半开区间[start, end)过滤。"""
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        # 9 月全月:start=2026-09-01T00:00:00+00:00, end=2026-10-01T00:00:00+00:00
        result = query_events(
            path,
            start="2026-09-01T00:00:00+00:00",
            end="2026-10-01T00:00:00+00:00",
        )
        assert result["total"] == 8  # 9 月 8 条
        for ev in result["events"]:
            assert ev["timestamp"].startswith("2026-09")

        # 仅 9 月 5-7 日 (B02 × 2 + C03 第 1 条)
        result2 = query_events(
            path,
            start="2026-09-05T00:00:00+00:00",
            end="2026-09-07T00:00:00+00:00",
        )
        assert result2["total"] == 2  # 9-05 (B02 search) + 9-06 (B02 apply)

    def test_query_events_date_range_accepts_datetime_objects(
        self, tmp_path: Path
    ):
        """start / end 支持 datetime 实例(便于 Python caller 不用先 to string)。"""
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(
            path,
            start=_dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc),
            end=_dt.datetime(2026, 10, 1, tzinfo=_dt.timezone.utc),
        )
        assert result["total"] == 8

    def test_query_events_combined_filters_and(self, tmp_path: Path):
        """多过滤条件按 AND 组合。"""
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(
            path,
            operator="pharmacist-A01",
            action="apply",
        )
        # A01 的 apply = 8 月 1 + 9 月 nsaid × 1 = 2
        assert result["total"] == 2
        for ev in result["events"]:
            assert ev["operator"] == "pharmacist-A01"
            assert ev["action"] == "apply"

    def test_query_events_pagination(self, tmp_path: Path):
        """page=1 / page=2 / page=3 拼接 = 全量(且无重叠)。"""
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        page_size = 5
        page1 = query_events(path, page=1, page_size=page_size)
        page2 = query_events(path, page=2, page_size=page_size)
        page3 = query_events(path, page=3, page_size=page_size)

        assert page1["total"] == 12
        assert page1["page_size"] == page_size
        assert len(page1["events"]) == 5
        assert len(page2["events"]) == 5
        assert len(page3["events"]) == 2  # 12 - 5 - 5

        # 三页无重叠(按 operator/rule_id/timestamp 拼起来的 key 集合应 disjoint)
        def _key(ev: dict) -> tuple:
            return (
                ev.get("timestamp"),
                ev.get("operator"),
                ev.get("rule_id"),
                ev.get("action"),
            )

        keys1 = {_key(ev) for ev in page1["events"]}
        keys2 = {_key(ev) for ev in page2["events"]}
        keys3 = {_key(ev) for ev in page3["events"]}
        assert keys1.isdisjoint(keys2)
        assert keys1.isdisjoint(keys3)
        assert keys2.isdisjoint(keys3)
        assert len(keys1 | keys2 | keys3) == 12

    def test_query_events_page_beyond_data_returns_empty(self, tmp_path: Path):
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(path, page=99, page_size=10)
        assert result["total"] == 12
        assert result["events"] == []

    def test_query_events_page_size_clamped(self, tmp_path: Path):
        """page_size > 500 自动 clamp 到 500;page < 1 clamp 到 1。"""
        from pass_field_check.audit import query_events

        path = self._seed_mixed_audit(tmp_path)
        result = query_events(path, page=0, page_size=9999)
        assert result["page"] == 1
        assert result["page_size"] == 500

    def test_query_events_missing_file_returns_empty(self, tmp_path: Path):
        """audit.jsonl 不存在时返回空结果(便于 REST 端 200 + 空列表)。"""
        from pass_field_check.audit import query_events

        result = query_events(tmp_path / "missing.jsonl")
        assert result == {
            "events": [],
            "total": 0,
            "page": 1,
            "page_size": 50,
        }

    def test_query_events_skips_malformed_lines(self, tmp_path: Path):
        """空行 / 非法 JSON 行静默跳过(与 _iter_audit_records 行为一致)。"""
        from pass_field_check.audit import query_events

        path = tmp_path / "audit.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(_make_event(operator="ok-1")) + "\n")
            fh.write("\n")
            fh.write("{not json}\n")
            fh.write(json.dumps(_make_event(operator="ok-2")) + "\n")
        result = query_events(path)
        assert result["total"] == 2
        assert [ev["operator"] for ev in result["events"]] == ["ok-1", "ok-2"]


# ---------------------------------------------------------------------------
# archive_month — 月度 gzip 归档 + JSONL 瘦身
# ---------------------------------------------------------------------------


class TestArchiveMonth:
    def _seed_three_months(self, tmp_path: Path) -> Path:
        """构造跨 3 月的 7 条事件(8 月 2 + 9 月 4 + 10 月 1)。"""
        path = tmp_path / "audit.jsonl"
        aug = "2026-08-15T08:00:00+00:00"
        sep = "2026-09-08T08:00:00+00:00"
        octo = "2026-10-01T08:00:00+00:00"
        # 8 月 2 条
        append_audit(_make_event(timestamp=aug, operator="aug-1"), path)
        append_audit(_make_event(timestamp=aug, operator="aug-2"), path)
        # 9 月 4 条
        for i in range(4):
            append_audit(
                _make_event(
                    timestamp=sep, operator=f"sep-{i}", action="apply"
                ),
                path,
            )
        # 10 月 1 条
        append_audit(_make_event(timestamp=octo, operator="oct-1"), path)
        return path

    def test_archive_month_basic_shrinks_audit_jsonl(
        self, tmp_path: Path
    ):
        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        result = archive_month("2026-09", path, archive_dir)

        # 返回 dict 四键
        assert result["month"] == "2026-09"
        assert result["archived_count"] == 4
        assert result["remaining_count"] == 3  # 8 月 2 + 10 月 1
        assert result["archive_path"].endswith("audit-2026-09.jsonl.gz")

        # 归档文件存在
        archive_path = Path(result["archive_path"])
        assert archive_path.exists()
        assert archive_path.parent == archive_dir.resolve()

        # audit.jsonl 瘦身:仅剩 3 条 (8 月 2 + 10 月 1)
        rows = _read_jsonl(path)
        assert len(rows) == 3
        assert all(
            r["timestamp"].startswith("2026-08")
            or r["timestamp"].startswith("2026-10")
            for r in rows
        )

    def test_archive_month_gz_file_content_is_valid_jsonl(
        self, tmp_path: Path
    ):
        """归档 .gz 解压后仍是合法 JSONL,字段顺序与原文件一致。"""
        import gzip

        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        result = archive_month("2026-09", path, archive_dir)
        archive_path = Path(result["archive_path"])

        with gzip.open(archive_path, "rt", encoding="utf-8") as fh:
            archived_lines = [line for line in fh if line.strip()]
        assert len(archived_lines) == 4
        decoded = [json.loads(line) for line in archived_lines]
        # 9 月 4 条 operator 倒序拼接应该都是 sep-*
        assert all(r["operator"].startswith("sep-") for r in decoded)
        assert all(r["timestamp"].startswith("2026-09") for r in decoded)
        # ensure_ascii=False:中文 operator 不应被 \u 转义
        appended_chinese_path = tmp_path / "audit_zh.jsonl"
        append_audit(
            _make_event(
                timestamp="2026-09-08T10:00:00+00:00",
                operator="药师甲",
                extra={"evidence_excerpt": "儿童应避免"},
            ),
            appended_chinese_path,
        )
        archive_zh_dir = tmp_path / "archive_zh"
        result_zh = archive_month(
            "2026-09", appended_chinese_path, archive_zh_dir
        )
        with gzip.open(
            Path(result_zh["archive_path"]), "rt", encoding="utf-8"
        ) as fh:
            raw = fh.read()
        assert "药师甲" in raw
        assert "儿童应避免" in raw

    def test_archive_month_empty_month_no_archive_file(
        self, tmp_path: Path
    ):
        """当月无事件 → 不创建 .gz 文件,返回 archived_count=0。"""
        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        # 12 月一条都没有
        result = archive_month("2026-12", path, archive_dir)
        assert result["archived_count"] == 0
        assert result["remaining_count"] == 7  # 没动
        # archive 文件不存在
        assert not (archive_dir / "audit-2026-12.jsonl.gz").exists()

    def test_archive_month_missing_audit_file_returns_zero(
        self, tmp_path: Path
    ):
        """audit.jsonl 不存在时静默返回 0,无 .gz 归档文件。"""
        from pass_field_check.audit import archive_month

        path = tmp_path / "missing.jsonl"
        archive_dir = tmp_path / "archive"
        result = archive_month("2026-09", path, archive_dir)
        assert result["archived_count"] == 0
        assert result["remaining_count"] == 0
        # archive_dir 入口会 mkdir -p(便于后续写入);但 .gz 文件不应被创建
        assert not (archive_dir / "audit-2026-09.jsonl.gz").exists()

    def test_archive_month_overwrite_false_raises_when_archive_exists(
        self, tmp_path: Path
    ):
        """已存在归档且 overwrite=False 时,新归档请求 raise FileExistsError。

        测试手法:先归档一次 9 月,然后把 9 月事件重新写回 audit.jsonl(模拟
        "又来了一批 9 月事件")+ 再次调用 archive_month(9 月),此时归档文件
        仍存在,应触发 FileExistsError。
        """
        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        # 第一次归档 9 月 → audit.jsonl 仅剩 8 月 + 10 月
        result1 = archive_month("2026-09", path, archive_dir)
        assert result1["archived_count"] == 4
        # 重新追加 9 月事件 → 模拟"系统生成新事件后又到月初"
        for ts, op in [
            ("2026-09-08T08:00:00+00:00", "new-a"),
            ("2026-09-09T08:00:00+00:00", "new-b"),
        ]:
            append_audit(_make_event(timestamp=ts, operator=op), path)
        # 第二次归档 9 月 → .gz 已存在,默认 overwrite=False 应 raise
        with pytest.raises(FileExistsError):
            archive_month("2026-09", path, archive_dir)

    def test_archive_month_overwrite_true_replaces_archive(
        self, tmp_path: Path
    ):
        """overwrite=True 时覆盖归档;审计场景默认拒绝覆盖。"""
        import gzip

        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        archive_month("2026-09", path, archive_dir)
        # 重置 audit.jsonl 让 9 月重新出现
        for ts, op in [
            ("2026-09-08T08:00:00+00:00", "res-1"),
            ("2026-09-08T09:00:00+00:00", "res-2"),
            ("2026-09-08T10:00:00+00:00", "res-3"),
        ]:
            append_audit(_make_event(timestamp=ts, operator=op), path)
        result = archive_month(
            "2026-09", path, archive_dir, overwrite=True
        )
        assert result["archived_count"] == 3
        # 归档文件覆盖后只剩 3 条
        archive_path = Path(result["archive_path"])
        with gzip.open(archive_path, "rt", encoding="utf-8") as fh:
            archived_lines = [line for line in fh if line.strip()]
        assert len(archived_lines) == 3

    def test_archive_month_bad_month_raises(self, tmp_path: Path):
        from pass_field_check.audit import archive_month

        path = tmp_path / "audit.jsonl"
        append_audit(_make_event(), path)
        archive_dir = tmp_path / "archive"
        with pytest.raises(ValueError):
            archive_month("2026/09", path, archive_dir)
        with pytest.raises(ValueError):
            archive_month("26-09", path, archive_dir)
        with pytest.raises(ValueError):
            archive_month("2026-13", path, archive_dir)

    def test_archive_month_creates_archive_dir(self, tmp_path: Path):
        """archive_dir 不存在时自动创建。"""
        from pass_field_check.audit import archive_month

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "deep" / "nested" / "archive"
        assert not archive_dir.exists()
        archive_month("2026-09", path, archive_dir)
        assert archive_dir.exists()
        assert (archive_dir / "audit-2026-09.jsonl.gz").exists()

    def test_archive_month_roundtrip_via_query_events(
        self, tmp_path: Path
    ):
        """归档后剩余事件仍可被 query_events 查到,无 schema drift。"""
        from pass_field_check.audit import archive_month, query_events

        path = self._seed_three_months(tmp_path)
        archive_dir = tmp_path / "archive"
        archive_month("2026-09", path, archive_dir)

        # 剩余 7 条 → 3 条;按月查询应匹配
        all_remaining = query_events(path)
        assert all_remaining["total"] == 3
        # 9 月已归档,半开区间过滤必须用 timezone-aware ISO 字符串
        sep_after = query_events(
            path,
            start="2026-09-01T00:00:00+00:00",
            end="2026-10-01T00:00:00+00:00",
        )
        assert sep_after["total"] == 0
        oct_after = query_events(
            path,
            start="2026-10-01T00:00:00+00:00",
            end="2026-11-01T00:00:00+00:00",
        )
        assert oct_after["total"] == 1


# ---------------------------------------------------------------------------
# rotate_if_oversize — 体积守门
# ---------------------------------------------------------------------------


class TestRotateIfOversize:
    def test_rotate_no_action_when_under_threshold(self, tmp_path: Path):
        """audit.jsonl 体积小于 max_mb → 不归档,返回 rotated=False。"""
        from pass_field_check.audit import rotate_if_oversize

        path = tmp_path / "audit.jsonl"
        append_audit(_make_event(), path)
        result = rotate_if_oversize(
            path, tmp_path / "archive", max_mb=50.0
        )
        assert result["rotated"] is False
        assert result["size_mb"] < 0.01
        assert result["month"] is None
        assert result["archived_count"] == 0

    def test_rotate_no_action_when_audit_missing(self, tmp_path: Path):
        from pass_field_check.audit import rotate_if_oversize

        result = rotate_if_oversize(
            tmp_path / "missing.jsonl", tmp_path / "archive", max_mb=50.0
        )
        assert result["rotated"] is False
        assert result["size_mb"] == 0.0
        assert result["month"] is None

    def test_rotate_archives_oldest_month_when_oversize(
        self, tmp_path: Path
    ):
        """构造大文件触发 rotate,断言归档最老一月 + audit.jsonl 瘦身。"""
        from pass_field_check.audit import rotate_if_oversize

        path = tmp_path / "audit.jsonl"
        # 跨 3 月构造 50KB + 数据,远超 max_mb=0.001 (≈1KB)
        # 每条约 0.5KB,120 条 ≈ 60KB
        for month in ("2026-06", "2026-07", "2026-08"):
            for day in range(1, 11):  # 10 天
                for op in range(4):  # 每天 4 条
                    ts = f"{month}-{day:02d}T08:00:00+00:00"
                    append_audit(
                        _make_event(
                            timestamp=ts,
                            operator=f"ph-{month}-{day:02d}-{op}",
                            extra={
                                "evidence_excerpt": "x" * 200,
                                "scores_top_n": [0.9] * 10,
                            },
                        ),
                        path,
                    )
        size_bytes = path.stat().st_size
        assert size_bytes > 50_000  # ≥50KB

        result = rotate_if_oversize(
            path, tmp_path / "archive", max_mb=0.001
        )
        assert result["rotated"] is True
        assert result["size_mb"] > 0.001
        assert result["month"] == "2026-06"  # 最老一月
        assert result["archived_count"] == 40  # 6 月 10 天 × 4 条
        assert result["archive_path"].endswith("audit-2026-06.jsonl.gz")

        # audit.jsonl 仅剩 7 月 + 8 月 = 80 条
        rows = _read_jsonl(path)
        assert len(rows) == 80

    def test_rotate_archive_exists_returns_error_marker(
        self, tmp_path: Path
    ):
        """归档已存在时,rotate 安全跳过并标记 error=archive_exists。

        测试手法:
        1. 构造跨 6/7 月的 audit.jsonl,体积超阈值
        2. 手动 archive_month("2026-06") → archive_dir/audit-2026-06.jsonl.gz 存在
        3. 在 audit.jsonl 中追加 6 月事件(让 6 月再次成为「最老一月」)
        4. rotate_if_oversize(超阈值)→ 尝试归档 6 月,但 .gz 已存在 → 拒绝
        """
        from pass_field_check.audit import (
            archive_month,
            rotate_if_oversize,
        )

        path = tmp_path / "audit.jsonl"
        archive_dir = tmp_path / "archive"
        # 跨 6/7 月构造数据
        for month in ("2026-06", "2026-07"):
            for day in range(1, 11):
                for op in range(4):
                    ts = f"{month}-{day:02d}T08:00:00+00:00"
                    append_audit(
                        _make_event(
                            timestamp=ts,
                            operator=f"ph-{month}-{day:02d}-{op}",
                            extra={"evidence_excerpt": "x" * 200},
                        ),
                        path,
                    )
        # 手动归档 6 月 → audit.jsonl 仅剩 7 月
        archive_month("2026-06", path, archive_dir)
        rows_after = _read_jsonl(path)
        assert len(rows_after) == 40  # 7 月 10 × 4

        # 追加 6 月事件(模拟"6 月数据又被写回"),让 6 月重新成为最老一月
        for day in (5, 6):
            for op in range(4):
                ts = f"2026-06-{day:02d}T08:00:00+00:00"
                append_audit(
                    _make_event(
                        timestamp=ts,
                        operator=f"late-{day}-{op}",
                        extra={"evidence_excerpt": "x" * 200},
                    ),
                    path,
                )
        # 触发 rotate → 试图归档 6 月,但 .gz 已存在 → 拒绝
        result = rotate_if_oversize(
            path, archive_dir, max_mb=0.001
        )
        assert result["rotated"] is False
        assert result["error"] == "archive_exists"
        assert result["month"] == "2026-06"
        # audit.jsonl 不变(7 月 + 6 月晚到 = 48 条)
        rows = _read_jsonl(path)
        assert len(rows) == 48

    def test_rotate_custom_max_mb_threshold(self, tmp_path: Path):
        """max_mb=0.001 (≈1KB) 在 ≥50KB 数据下必然触发 rotate。"""
        from pass_field_check.audit import rotate_if_oversize

        path = tmp_path / "audit.jsonl"
        # 单条记录 evidence_excerpt=200 字 + 长字段 → 单条约 0.5KB
        for month in ("2026-06", "2026-07"):
            for day in range(1, 11):
                for op in range(4):
                    ts = f"{month}-{day:02d}T08:00:00+00:00"
                    append_audit(
                        _make_event(
                            timestamp=ts,
                            operator=f"op-{op}",
                            extra={"evidence_excerpt": "y" * 300},
                        ),
                        path,
                    )
        # 80 条 × ~0.7KB ≈ 56KB
        size_mb = path.stat().st_size / (1024 * 1024)
        assert size_mb > 0.04  # >40KB

        result = rotate_if_oversize(path, tmp_path / "archive", max_mb=0.04)
        # max_mb=0.04 → 体积 ≥40KB 时触发
        if size_mb > 0.04:
            assert result["rotated"] is True
        else:
            assert result["rotated"] is False

    def test_rotate_no_months_returns_false(self, tmp_path: Path):
        """audit.jsonl 全是非 ISO 时间戳 → list_archived_months 空 → 不归档。"""
        from pass_field_check.audit import rotate_if_oversize

        path = tmp_path / "audit.jsonl"
        # 直接写一条没有 timestamp 字段的记录(append_audit 会自动补 timestamp,
        # 故此处用底层 IO 绕过)
        raw = json.dumps({"operator": "no-ts", "action": "search", "foo": "bar"})
        with path.open("w", encoding="utf-8") as fh:
            fh.write(raw + "\n")
            # 把文件撑大到 >0.001MB (≈1KB)
            fh.write(" " * 2000)
        result = rotate_if_oversize(
            path, tmp_path / "archive", max_mb=0.001
        )
        assert result["rotated"] is False
        assert result["month"] is None


# ---------------------------------------------------------------------------
# list_archived_months — 列出 audit.jsonl 中出现的月份(用于 rotate 选最老)
# ---------------------------------------------------------------------------


class TestListArchivedMonths:
    def test_list_returns_sorted_unique_months(self, tmp_path: Path):
        from pass_field_check.audit import list_archived_months

        path = tmp_path / "audit.jsonl"
        # 故意打乱顺序 + 重复
        for ts, op in [
            ("2026-09-08T08:00:00+00:00", "a"),
            ("2026-08-15T08:00:00+00:00", "b"),
            ("2026-09-08T09:00:00+00:00", "c"),
            ("2026-10-01T08:00:00+00:00", "d"),
            ("2026-08-16T08:00:00+00:00", "e"),
        ]:
            append_audit(_make_event(timestamp=ts, operator=op), path)
        months = list_archived_months(path)
        assert months == ["2026-08", "2026-09", "2026-10"]

    def test_list_missing_file_returns_empty(self, tmp_path: Path):
        from pass_field_check.audit import list_archived_months

        assert list_archived_months(tmp_path / "missing.jsonl") == []

    def test_list_skips_non_iso_timestamps(self, tmp_path: Path):
        from pass_field_check.audit import list_archived_months

        path = tmp_path / "audit.jsonl"
        append_audit(
            {
                "timestamp": "not-iso",
                "operator": "x",
                "action": "search",
            },
            path,
        )
        append_audit(
            _make_event(timestamp="2026-09-08T08:00:00+00:00", operator="y"),
            path,
        )
        assert list_archived_months(path) == ["2026-09"]


# ---------------------------------------------------------------------------
# CLI 子命令:audit-archive / audit-rotate
# ---------------------------------------------------------------------------


class TestAuditCliSubcommands:
    def test_audit_archive_subcommand_succeeds(self, tmp_path: Path):
        """CLI pass-fc audit-archive --month --archive-dir 调用正常。"""
        import subprocess
        import sys

        audit_path = tmp_path / "audit.jsonl"
        for ts, op in [
            ("2026-09-08T08:00:00+00:00", "a"),
            ("2026-09-08T09:00:00+00:00", "b"),
        ]:
            append_audit(_make_event(timestamp=ts, operator=op), audit_path)
        archive_dir = tmp_path / "archive"

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pass_field_check.cli",
                "audit-archive",
                "--month",
                "2026-09",
                "--audit-path",
                str(audit_path),
                "--archive-dir",
                str(archive_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert "archived_count" in proc.stdout
        assert (archive_dir / "audit-2026-09.jsonl.gz").exists()

    def test_audit_rotate_subcommand_fires_when_oversize(
        self, tmp_path: Path
    ):
        import subprocess
        import sys

        audit_path = tmp_path / "audit.jsonl"
        for month in ("2026-06", "2026-07"):
            for day in range(1, 11):
                for op in range(4):
                    ts = f"{month}-{day:02d}T08:00:00+00:00"
                    append_audit(
                        _make_event(
                            timestamp=ts,
                            operator=f"ph-{month}-{day:02d}-{op}",
                            extra={"evidence_excerpt": "x" * 300},
                        ),
                        audit_path,
                    )

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pass_field_check.cli",
                "audit-rotate",
                "--audit-path",
                str(audit_path),
                "--archive-dir",
                str(tmp_path / "archive"),
                "--max-mb",
                "0.04",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        # 体积超过 0.04MB → rotate 触发 → exit 0 + stdout 含 rotated=true
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert "rotated" in proc.stdout

    def test_audit_rotate_subcommand_no_action_exit_zero(
        self, tmp_path: Path
    ):
        """未超阈值时 exit 0 + rotated=false,便于 cron 静默调用。"""
        import subprocess
        import sys

        audit_path = tmp_path / "audit.jsonl"
        append_audit(_make_event(), audit_path)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pass_field_check.cli",
                "audit-rotate",
                "--audit-path",
                str(audit_path),
                "--archive-dir",
                str(tmp_path / "archive"),
                "--max-mb",
                "50.0",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == 0
        assert "rotated" in proc.stdout

    def test_audit_archive_duplicate_raises_exit_3(self, tmp_path: Path):
        """重复归档同月 → 退出码 3(FileExistsError 路径)。"""
        import subprocess
        import sys

        audit_path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(timestamp="2026-09-08T08:00:00+00:00", operator="a"),
            audit_path,
        )
        archive_dir = tmp_path / "archive"
        # 第一次 OK
        proc1 = subprocess.run(
            [
                sys.executable,
                "-m",
                "pass_field_check.cli",
                "audit-archive",
                "--month",
                "2026-09",
                "--audit-path",
                str(audit_path),
                "--archive-dir",
                str(archive_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc1.returncode == 0
        # 第二次归档同月 → exit 3
        # 先恢复 audit.jsonl 让 9 月再出现
        append_audit(
            _make_event(timestamp="2026-09-09T08:00:00+00:00", operator="b"),
            audit_path,
        )
        proc2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "pass_field_check.cli",
                "audit-archive",
                "--month",
                "2026-09",
                "--audit-path",
                str(audit_path),
                "--archive-dir",
                str(archive_dir),
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc2.returncode == 3
        assert "error" in proc2.stderr.lower()

# ---------------------------------------------------------------------------
# monthly_summary / _format_summary_markdown (task 31)
# ---------------------------------------------------------------------------


def _write_rules_index(path: Path, mapping: dict[str, str]) -> Path:
    """写一份最小 data/rules.json,只含 monthly_summary 需要的 rule_id/severity。"""
    payload = {
        "version": "2026.09.08",
        "rules": [
            {"rule_id": rid, "severity": sev, "drug_class": "抗菌药"}
            for rid, sev in mapping.items()
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestMonthlySummary:
    def test_monthly_summary_top_rules(self, tmp_path: Path):
        """30 条事件跨 3 个 rule_id → top_triggered_rules 按计数倒序。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        # 15 / 10 / 5 条,便于断言严格倒序
        plan = [
            ("rx-aminoglycoside-pediatric", 15),
            ("rx-nsaid-pregnancy", 10),
            ("rx-ppi-longterm", 5),
        ]
        day = 1
        for rule_id, times in plan:
            for _ in range(times):
                append_audit(
                    _make_event(
                        rule_id=rule_id,
                        timestamp=f"2026-09-{day:02d}T09:00:00+00:00",
                    ),
                    audit_path,
                )
                day = day % 28 + 1

        summary = monthly_summary("2026-09", audit_path)
        assert summary["total_events"] == 30
        assert summary["distinct_rules"] == 3
        top = summary["top_triggered_rules"]
        assert [t["rule_id"] for t in top] == [
            "rx-aminoglycoside-pediatric",
            "rx-nsaid-pregnancy",
            "rx-ppi-longterm",
        ]
        assert [t["count"] for t in top] == [15, 10, 5]

    def test_monthly_summary_top_n_truncates(self, tmp_path: Path):
        """12 个不同 rule_id → 默认 top_n=10 截断。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        for i in range(12):
            for _ in range(12 - i):  # 计数递减,保证顺序确定
                append_audit(
                    _make_event(
                        rule_id=f"rx-rule-{i:02d}",
                        timestamp="2026-09-10T09:00:00+00:00",
                    ),
                    audit_path,
                )
        summary = monthly_summary("2026-09", audit_path)
        assert summary["distinct_rules"] == 12
        assert len(summary["top_triggered_rules"]) == 10
        assert summary["top_triggered_rules"][0]["rule_id"] == "rx-rule-00"

    def test_monthly_summary_operator_distribution(self, tmp_path: Path):
        """4 个 operator 命中 → operator_distribution 计数正确且倒序。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        plan = {
            "pharmacist-001": 4,
            "pharmacist-002": 3,
            "pharmacist-003": 2,
            "pharmacist-004": 1,
        }
        for operator, times in plan.items():
            for _ in range(times):
                append_audit(
                    _make_event(
                        operator=operator,
                        timestamp="2026-09-11T10:00:00+00:00",
                    ),
                    audit_path,
                )
        summary = monthly_summary("2026-09", audit_path)
        assert summary["distinct_operators"] == 4
        dist = {item["key"]: item["count"] for item in summary["operator_distribution"]}
        assert dist == plan
        # 倒序断言
        counts = [item["count"] for item in summary["operator_distribution"]]
        assert counts == sorted(counts, reverse=True)

    def test_monthly_summary_severity_distribution(self, tmp_path: Path):
        """join rules.json 取 severity → 按 high/medium/low 固定序输出。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        rules_path = _write_rules_index(
            tmp_path / "data" / "rules.json",
            {
                "rx-aminoglycoside-pediatric": "high",
                "rx-metformin-renal": "medium",
                "rx-ppi-longterm": "low",
            },
        )
        for rule_id, times in [
            ("rx-aminoglycoside-pediatric", 3),
            ("rx-metformin-renal", 2),
            ("rx-ppi-longterm", 1),
        ]:
            for _ in range(times):
                append_audit(
                    _make_event(
                        rule_id=rule_id, timestamp="2026-09-12T08:00:00+00:00"
                    ),
                    audit_path,
                )
        summary = monthly_summary("2026-09", audit_path, rules_path)
        assert summary["severity_joined"] is True
        assert summary["severity_distribution"] == [
            {"key": "high", "count": 3},
            {"key": "medium", "count": 2},
            {"key": "low", "count": 1},
        ]
        # top_triggered_rules 也带上 severity,便于简报直接渲染
        assert summary["top_triggered_rules"][0]["severity"] == "high"

    def test_monthly_summary_severity_unknown_without_rules(self, tmp_path: Path):
        """未提供 rules.json → severity 归 unknown,severity_joined=False。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(timestamp="2026-09-13T08:00:00+00:00"), audit_path
        )
        summary = monthly_summary("2026-09", audit_path)
        assert summary["severity_joined"] is False
        assert summary["severity_distribution"] == [{"key": "unknown", "count": 1}]

    def test_monthly_summary_confirmation_rate(self, tmp_path: Path):
        """确认率 = confirmed apply / apply 总数,取值在 [0.0, 1.0]。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        # 4 条 apply(其中 1 条 confirmed=True)+ 2 条 search(不计入分母)
        for confirmed in (True, False, False, False):
            append_audit(
                _make_event(
                    action="apply",
                    confirmed=confirmed,
                    timestamp="2026-09-14T08:00:00+00:00",
                ),
                audit_path,
            )
        for _ in range(2):
            append_audit(
                _make_event(
                    action="search", timestamp="2026-09-14T08:30:00+00:00"
                ),
                audit_path,
            )
        summary = monthly_summary("2026-09", audit_path)
        assert summary["apply_count"] == 4
        assert summary["confirmed_count"] == 1
        assert summary["unconfirmed_count"] == 3
        assert summary["confirmation_rate"] == 0.25
        assert 0.0 <= summary["confirmation_rate"] <= 1.0
        actions = {i["key"]: i["count"] for i in summary["action_distribution"]}
        assert actions == {"apply": 4, "search": 2}

    def test_monthly_summary_confirmation_rate_zero_applies(self, tmp_path: Path):
        """当月只有 search 事件 → 确认率 0.0(不除零)。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(action="search", timestamp="2026-09-15T08:00:00+00:00"),
            audit_path,
        )
        summary = monthly_summary("2026-09", audit_path)
        assert summary["apply_count"] == 0
        assert summary["confirmation_rate"] == 0.0

    def test_monthly_summary_daily_activity_and_month_filter(self, tmp_path: Path):
        """daily_activity 按日期升序;上月事件不计入本月简报。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        for ts, times in [
            ("2026-09-03T08:00:00+00:00", 2),
            ("2026-09-01T08:00:00+00:00", 3),
            ("2026-08-31T08:00:00+00:00", 5),  # 上月,应被过滤
        ]:
            for _ in range(times):
                append_audit(_make_event(timestamp=ts), audit_path)

        summary = monthly_summary("2026-09", audit_path)
        assert summary["total_events"] == 5
        assert summary["daily_activity"] == [
            {"date": "2026-09-01", "count": 3},
            {"date": "2026-09-03", "count": 2},
        ]

    def test_monthly_summary_missing_audit_file(self, tmp_path: Path):
        """audit.jsonl 不存在 → 全零简报,不 raise(新院区当月无留痕)。"""
        from pass_field_check.audit import monthly_summary

        summary = monthly_summary("2026-09", tmp_path / "nope.jsonl")
        assert summary["total_events"] == 0
        assert summary["top_triggered_rules"] == []
        assert summary["confirmation_rate"] == 0.0

    def test_monthly_summary_bad_month_raises(self, tmp_path: Path):
        from pass_field_check.audit import monthly_summary

        with pytest.raises(ValueError):
            monthly_summary("2026/09", tmp_path / "audit.jsonl")

    def test_monthly_summary_is_deterministic(self, tmp_path: Path):
        """同一份 audit 重复聚合结果完全一致(便于简报留档 diff)。"""
        from pass_field_check.audit import monthly_summary

        audit_path = tmp_path / "audit.jsonl"
        for i in range(6):
            append_audit(
                _make_event(
                    rule_id=f"rx-rule-{i % 3}",
                    operator=f"op-{i % 2}",
                    timestamp="2026-09-16T08:00:00+00:00",
                ),
                audit_path,
            )
        first = monthly_summary("2026-09", audit_path)
        second = monthly_summary("2026-09", audit_path)
        assert first == second


class TestFormatSummaryMarkdown:
    def test_format_summary_markdown_tables(self, tmp_path: Path):
        """markdown 简报含全部章节表头 + 规则 / 操作者 / 确认率。"""
        from pass_field_check.audit import (
            _format_summary_markdown,
            monthly_summary,
        )

        audit_path = tmp_path / "audit.jsonl"
        rules_path = _write_rules_index(
            tmp_path / "data" / "rules.json",
            {"rx-aminoglycoside-pediatric": "high", "rx-ppi-longterm": "low"},
        )
        append_audit(
            _make_event(
                rule_id="rx-aminoglycoside-pediatric",
                operator="pharmacist-001",
                action="apply",
                confirmed=True,
                timestamp="2026-09-17T08:00:00+00:00",
            ),
            audit_path,
        )
        append_audit(
            _make_event(
                rule_id="rx-ppi-longterm",
                operator="pharmacist-002",
                action="apply",
                confirmed=False,
                timestamp="2026-09-18T08:00:00+00:00",
            ),
            audit_path,
        )
        text = _format_summary_markdown(
            monthly_summary("2026-09", audit_path, rules_path)
        )
        # 章节标题
        assert "月度审方简报(2026-09)" in text
        assert "## 概览" in text
        assert "## 高频命中规则" in text
        assert "## 操作者分布" in text
        assert "## 严重度分布" in text
        assert "## 每日活跃度" in text
        assert "## 口径说明" in text
        # 表头
        assert "| 排名 | 规则 | 严重度 | 命中次数 | 占比 |" in text
        assert "| 操作者 | 事件数 | 占比 |" in text
        assert "| 日期 | 事件数 | 分布 |" in text
        # 数据
        assert "rx-aminoglycoside-pediatric" in text
        assert "pharmacist-002" in text
        assert "50.0%" in text  # 确认率 1/2
        # ASCII 柱状
        assert "█" in text
        # 永不代签的口径说明
        assert "confirmed=false" in text

    def test_format_summary_markdown_empty_month(self, tmp_path: Path):
        """空月份仍输出标题 + 无留痕提示,不出现空表。"""
        from pass_field_check.audit import (
            _format_summary_markdown,
            monthly_summary,
        )

        text = _format_summary_markdown(
            monthly_summary("2026-01", tmp_path / "audit.jsonl")
        )
        assert "月度审方简报(2026-01)" in text
        assert "无审方留痕" in text
        assert "| 排名 |" not in text

    def test_format_summary_markdown_unknown_severity_hint(self, tmp_path: Path):
        """未 join rules.json 时,口径说明提示先跑 index-build。"""
        from pass_field_check.audit import (
            _format_summary_markdown,
            monthly_summary,
        )

        audit_path = tmp_path / "audit.jsonl"
        append_audit(
            _make_event(timestamp="2026-09-19T08:00:00+00:00"), audit_path
        )
        text = _format_summary_markdown(monthly_summary("2026-09", audit_path))
        assert "index-build" in text
