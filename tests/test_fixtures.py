"""Tests for tests/fixtures/ JSON fixtures.

These fixtures are *test doubles*, not the final HIS integration surface.
They exist so:
  - pytest can exercise apply / inspect / search code paths without
    touching real HIS prescription data;
  - field-name alignment to contract.SCHEMA_HIS_REVIEW_FIELD stays enforced
    so that any drift fails loudly here rather than during a real对接.

mock 替身边界(spec.md ## 对接层 显式声明):
  本测试文件不验证与真实 HIS 厂家的契约,仅验证本仓库内部 fixture 形态。
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from pass_field_check.contract import SCHEMA_HIS_REVIEW_FIELD

from conftest import load_fixture


# ---------------------------------------------------------------------------
# test_mock_prescriptions_count
# ---------------------------------------------------------------------------


def test_mock_prescriptions_count() -> None:
    """mock_prescriptions.json must contain at least 6 de-identified samples.

    Coverage: 庆大霉素儿童 / NSAID 孕期 / 二甲双胍肾损 / ACEI 孕期 /
    华法林 INR / PPI 长期 — Task 15 指定 6 个典型审方场景。
    """
    data = load_fixture("mock_prescriptions")
    prescriptions = data.get("prescriptions")

    assert isinstance(prescriptions, list), "prescriptions 字段必须是 list"
    assert len(prescriptions) >= 6, (
        f"mock_prescriptions 至少 6 条样本,实际 {len(prescriptions)}"
    )

    # 每条处方必须包含字段上下文字段,以便 apply/load 工具调 order_context
    required_fields = {
        "prescription_id",
        "patient_age",
        "pregnancy",
        "egfr",
        "drug_name",
        "dose",
        "route",
        "frequency",
        "department",
    }
    for idx, item in enumerate(prescriptions):
        missing = required_fields - set(item.keys())
        assert not missing, f"prescriptions[{idx}] 缺字段: {sorted(missing)}"

    # 覆盖 Task 15 指定的 6 个临床审方场景(药物通用名命中)
    scenarios = {
        "庆大霉素儿童": "庆大霉素",
        "NSAID 孕期": "布洛芬",
        "二甲双胍肾损": "二甲双胍",
        "ACEI 孕期": "卡托普利",
        "华法林 INR": "华法林",
        "PPI 长期": "奥美拉唑",
    }
    for label, drug in scenarios.items():
        matched = [p for p in prescriptions if drug in p.get("drug_name", "")]
        assert matched, (
            f"场景 '{label}' (drug={drug}) 未在 prescriptions 中命中"
        )


# ---------------------------------------------------------------------------
# test_mock_his_fields_contract
# ---------------------------------------------------------------------------


def test_mock_his_fields_contract() -> None:
    """mock_his_fields.json 字段名与 contract.SCHEMA_HIS_REVIEW_FIELD 对齐.

    不依赖 jsonschema(避免 fixture 里 64 hex / 16 hex 是占位符而非真实 sha256),
    只校验 key 集合 + 字段级最小形态(pattern / minLength / enum)。
    """
    data = load_fixture("mock_his_fields")
    items = data.get("his_review_fields")

    assert isinstance(items, list), "his_review_fields 字段必须是 list"
    assert len(items) >= 1, "mock_his_fields 至少 1 条样例"

    required = set(SCHEMA_HIS_REVIEW_FIELD["required"])
    additional = SCHEMA_HIS_REVIEW_FIELD.get("additionalProperties", True)
    rule_id_pattern = SCHEMA_HIS_REVIEW_FIELD["properties"]["rule_id"]["pattern"]
    order_hash_pattern = SCHEMA_HIS_REVIEW_FIELD["properties"]["order_hash"]["pattern"]
    session_id_pattern = SCHEMA_HIS_REVIEW_FIELD["properties"]["session_id"]["pattern"]
    operator_min = SCHEMA_HIS_REVIEW_FIELD["properties"]["operator"]["minLength"]
    evidence_max = SCHEMA_HIS_REVIEW_FIELD["properties"]["evidence_excerpt"]["maxLength"]
    confirmed_enum = SCHEMA_HIS_REVIEW_FIELD["properties"]["confirmed"]["enum"]

    for idx, item in enumerate(items):
        keys = set(item.keys())

        # 必填字段全部存在
        missing = required - keys
        assert not missing, f"his_review_fields[{idx}] 缺必填字段: {sorted(missing)}"

        # additionalProperties=False 时不允许额外字段
        if additional is False:
            extra = keys - required
            assert not extra, (
                f"his_review_fields[{idx}] 含 contract 未声明的额外字段: {sorted(extra)}"
            )

        # rule_id 必须 kebab-case
        assert re.fullmatch(rule_id_pattern, item["rule_id"]), (
            f"his_review_fields[{idx}] rule_id 不匹配 pattern: {item['rule_id']!r}"
        )

        # order_hash 必须 64 hex
        assert re.fullmatch(order_hash_pattern, item["order_hash"]), (
            f"his_review_fields[{idx}] order_hash 不是 64 hex"
        )

        # session_id 必须 16 hex
        assert re.fullmatch(session_id_pattern, item["session_id"]), (
            f"his_review_fields[{idx}] session_id 不是 16 hex"
        )

        # operator 必须非空字符串
        assert isinstance(item["operator"], str) and len(item["operator"]) >= operator_min, (
            f"his_review_fields[{idx}] operator 为空"
        )

        # applied_at 必须 ISO 8601 with timezone(从右解析 +Z / +HH:MM)
        applied_at = item["applied_at"]
        assert isinstance(applied_at, str)
        # fromisoformat 在 3.11+ 支持 Z;此处只校验格式可解析 + 含时区偏移
        normalized = applied_at.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            pytest.fail(f"his_review_fields[{idx}] applied_at 无法解析: {applied_at!r} ({exc})")
        assert parsed.tzinfo is not None, (
            f"his_review_fields[{idx}] applied_at 缺时区: {applied_at!r}"
        )

        # evidence_excerpt 不超 200 字
        excerpt = item["evidence_excerpt"]
        assert isinstance(excerpt, str)
        assert len(excerpt) <= evidence_max, (
            f"his_review_fields[{idx}] evidence_excerpt 超 {evidence_max} 字"
        )

        # confirmed 强制 False(本工具永不代签)
        assert item["confirmed"] in confirmed_enum, (
            f"his_review_fields[{idx}] confirmed 必须为 False(本工具永不代签)"
        )


# ---------------------------------------------------------------------------
# test_fixture_deident_note
# ---------------------------------------------------------------------------


def test_fixture_deident_note() -> None:
    """每条 fixture (mock_prescriptions / mock_his_fields) 顶层含 _deident_note.

    _deident_note 字段是测试替身身份的强制声明:提醒读者 fixture 仅用于
    pytest,不代表真实患者数据,不作为最终 HIS 对接。
    """
    for name in ("mock_prescriptions", "mock_his_fields"):
        data = load_fixture(name)
        note = data.get("_deident_note")
        assert isinstance(note, str), f"{name} 缺 _deident_note"
        assert note.strip(), f"{name} _deident_note 不能为空"
        # 强化语义:必须包含「mock」或「测试替身」字样,避免被误当作正式对接文档
        lowered = note.lower()
        assert ("mock" in lowered) or ("测试替身" in note) or ("脱敏" in note), (
            f"{name} _deident_note 必须显式声明 mock / 测试替身 / 脱敏语义"
        )