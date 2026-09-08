"""Tests for pass_field_check.tools -- 4 fixed tool schemas + register_rx_tools.

Task 8 spec:
  - 4 schemas 校验 (name / description / parameters type=object / properties / required)
  - register_rx_tools 把 4 tool 写入 ctx.tools,tool name 集合 ==
    {'rx_rule_search', 'rx_rule_inspect', 'rx_rule_load', 'rx_rule_apply'}
  - apply_rule 返回 dict 含 order_hash (sha256 64 字符) / session_id (16 字符) /
    confirmed=False / operator / applied_at (ISO 8601) / evidence_excerpt
  - apply_rule 缺 order_context / operator / 空 order_context → ValueError
  - audit append: 同 apply 写入 audit.jsonl,confirmed=False 必写
  - search→inspect 链路: search top1.rule_id == inspect.rule_id
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pass_field_check.index_builder import collect_rx_rules
from pass_field_check.tools import (
    RX_APPLY_SCHEMA,
    RX_INSPECT_SCHEMA,
    RX_LOAD_SCHEMA,
    RX_SEARCH_SCHEMA,
    RX_TOOL_SCHEMAS,
    apply_rule,
    bind_runtime,
    inspect_rule,
    load_rule,
    register_rx_tools,
    re_split_sentences,
)


# ---------------------------------------------------------------------------
# RecordingContext -- mirrors github_ref/agency-agents/scripts/check-hermes-plugin.py
# RecordingContext (L24-29): register_tool(name=...) 把记录塞进 self.tools 字典。
# 本实现的 ctx 还接受 toolset/schema/handler/description,方便断言 4 工具契约。
# ---------------------------------------------------------------------------


class RecordingContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(self, **kwargs: Any) -> None:
        # 镜像源产品的 ctx.register_tool(**kwargs) 调用方式
        self.tools[kwargs["name"]] = kwargs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rules_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "rules"


@pytest.fixture
def sample_rules(rules_dir: Path) -> list[dict[str, Any]]:
    return collect_rx_rules(rules_dir)


@pytest.fixture(autouse=True)
def _restore_bindings():
    """Snapshot and restore module-level _rules / _audit_path to avoid test pollution."""
    from pass_field_check import tools

    saved_rules = tools._rules
    saved_audit = tools._audit_path
    yield
    tools._rules = saved_rules
    tools._audit_path = saved_audit


# ---------------------------------------------------------------------------
# 1) Schema 校验: name / description / parameters(type=object, properties, required)
# ---------------------------------------------------------------------------


def test_4_schemas_have_required_fields():
    """Each schema must have name / description / parameters{type=object,properties,required}."""
    for schema in RX_TOOL_SCHEMAS:
        assert isinstance(schema, dict)
        assert "name" in schema, f"{schema.get('name')}: missing name"
        assert "description" in schema, f"{schema['name']}: missing description"
        params = schema.get("parameters")
        assert isinstance(params, dict), f"{schema['name']}: parameters must be dict"
        assert params.get("type") == "object", f"{schema['name']}: parameters.type must be object"
        assert isinstance(params.get("properties"), dict), f"{schema['name']}: properties missing"
        assert isinstance(params.get("required"), list), f"{schema['name']}: required must be list"


def test_search_schema_specifics():
    """RX_SEARCH_SCHEMA must declare query/drug_class/limit with query required."""
    params = RX_SEARCH_SCHEMA["parameters"]
    assert "query" in params["properties"]
    assert "drug_class" in params["properties"]
    assert "limit" in params["properties"]
    assert params["required"] == ["query"]
    # limit has minimum / maximum (mirrors source SEARCH_SCHEMA semantics)
    assert params["properties"]["limit"].get("minimum") == 1


def test_inspect_schema_specifics():
    """RX_INSPECT_SCHEMA: rule_id required, include_body optional."""
    params = RX_INSPECT_SCHEMA["parameters"]
    assert params["required"] == ["rule_id"]
    assert "include_body" in params["properties"]


def test_load_schema_specifics():
    """RX_LOAD_SCHEMA: rule_id and order_context required."""
    params = RX_LOAD_SCHEMA["parameters"]
    assert set(params["required"]) == {"rule_id", "order_context"}


def test_apply_schema_specifics():
    """RX_APPLY_SCHEMA: rule_id / order_context / operator all required."""
    params = RX_APPLY_SCHEMA["parameters"]
    assert set(params["required"]) == {"rule_id", "order_context", "operator"}


def test_4_schemas_validate_with_jsonschema():
    """Each schema must itself be a valid JSON Schema (Draft 2020-12)."""
    for schema in RX_TOOL_SCHEMAS:
        # Wrap as the meta-schema spec: schema["parameters"] is the actual schema.
        Draft202012Validator.check_schema(schema["parameters"])


def test_4_schemas_unique_names():
    """Tool names must be unique so register_rx_tools never overwrites itself."""
    names = [schema["name"] for schema in RX_TOOL_SCHEMAS]
    assert len(names) == len(set(names)) == 4
    assert set(names) == {
        "rx_rule_search",
        "rx_rule_inspect",
        "rx_rule_load",
        "rx_rule_apply",
    }


# ---------------------------------------------------------------------------
# 2) register_rx_tools: 4 tool 写入 ctx
# ---------------------------------------------------------------------------


def test_register_rx_tools_registers_four(sample_rules: list[dict[str, Any]]):
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)
    assert set(ctx.tools) == {
        "rx_rule_search",
        "rx_rule_inspect",
        "rx_rule_load",
        "rx_rule_apply",
    }


def test_register_rx_tools_each_has_schema_and_handler(sample_rules):
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)
    for name, registration in ctx.tools.items():
        assert registration.get("schema"), f"{name}: missing schema"
        assert registration.get("handler"), f"{name}: missing handler"
        assert registration.get("description"), f"{name}: missing description"
        assert registration.get("toolset") == "rx_field_check"


# ---------------------------------------------------------------------------
# 3) apply_rule 返回值与哈希 / session_id / confirmed 强制 False
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_order_context() -> dict[str, Any]:
    return {
        "patient_age": 8,
        "pregnancy": False,
        "egfr": 110,
        "drug_name": "庆大霉素",
        "dose": "80mg",
        "route": "iv",
        "frequency": "q8h",
        "department": "儿科",
    }


def test_apply_rule_returned_fields(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """apply_rule returns dict with order_hash (64 chars), session_id (16 chars),
    confirmed=False, operator, applied_at, evidence_excerpt."""
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    assert isinstance(payload, dict)
    assert payload["rule_id"] == "rx-aminoglycoside-pediatric"
    assert re.fullmatch(r"[0-9a-f]{64}", payload["order_hash"]), payload["order_hash"]
    assert re.fullmatch(r"[0-9a-f]{16}", payload["session_id"]), payload["session_id"]
    assert payload["confirmed"] is False
    assert payload["operator"] == "pharmacist-001"
    assert "applied_at" in payload
    assert "T" in payload["applied_at"]  # ISO 8601
    assert isinstance(payload["evidence_excerpt"], str)
    assert payload["evidence_excerpt"], "evidence_excerpt must not be empty"


def test_apply_rule_missing_operator_raises(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    with pytest.raises(ValueError, match="operator"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            sample_order_context,
            sample_rules,
            operator="",
        )


def test_apply_rule_missing_order_context_raises(sample_rules: list[dict[str, Any]]):
    with pytest.raises(ValueError, match="order_context"):
        apply_rule("rx-aminoglycoside-pediatric", {}, sample_rules, operator="x")


def test_apply_rule_non_dict_order_context_raises(sample_rules: list[dict[str, Any]]):
    with pytest.raises(ValueError, match="order_context"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            "not a dict",  # type: ignore[arg-type]
            sample_rules,
            operator="x",
        )


def test_apply_rule_unknown_rule_id_raises(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    with pytest.raises(ValueError, match="rule_id 未找到"):
        apply_rule(
            "rx-does-not-exist",
            sample_order_context,
            sample_rules,
            operator="x",
        )


def test_apply_rule_idempotent_order_hash(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """Same order_context → same order_hash (canonical sort_keys)."""
    a = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    # 字段顺序不同的同语义 dict → 同样 hash
    swapped = dict(reversed(list(sample_order_context.items())))
    b = apply_rule(
        "rx-aminoglycoside-pediatric",
        swapped,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["order_hash"] == b["order_hash"]
    assert a["session_id"] == b["session_id"]


def test_apply_rule_audit_appends_jsonl(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
    tmp_path: Path,
):
    audit_path = tmp_path / "audit" / "audit.jsonl"
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
        audit_path=audit_path,
    )
    assert audit_path.exists()
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["rule_id"] == payload["rule_id"]
    assert row["order_hash"] == payload["order_hash"]
    assert row["session_id"] == payload["session_id"]
    assert row["operator"] == payload["operator"]
    assert row["confirmed"] is False
    assert row.get("action") == "apply"


# ---------------------------------------------------------------------------
# 4) inspect_rule / load_rule
# ---------------------------------------------------------------------------


def test_inspect_rule_includes_body(sample_rules):
    payload = inspect_rule("rx-aminoglycoside-pediatric", sample_rules)
    assert payload["success"] is True
    assert payload["rule"]["rule_id"] == "rx-aminoglycoside-pediatric"
    assert "依据片段" in payload["body"]


def test_inspect_rule_excludes_body_when_false(sample_rules):
    payload = inspect_rule(
        "rx-aminoglycoside-pediatric", sample_rules, include_body=False
    )
    assert "body" not in payload


def test_inspect_rule_not_found_raises(sample_rules):
    with pytest.raises(ValueError, match="rule_id 未找到"):
        inspect_rule("rx-nope", sample_rules)


def test_load_rule_composes_draft(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    payload = load_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
    )
    assert payload["success"] is True
    assert payload["rule"]["rule_id"] == "rx-aminoglycoside-pediatric"
    assert payload["order_context"] == sample_order_context
    assert isinstance(payload["evidence_excerpt"], str)
    assert payload["evidence_excerpt"]


# ---------------------------------------------------------------------------
# 5) search→inspect 链路 + handler smoke
# ---------------------------------------------------------------------------


def test_search_then_inspect_chain_via_handlers(sample_rules):
    """search top-1 rule_id 必须能送进 inspect 并命中同一条规则。"""
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)

    search_result = ctx.tools["rx_rule_search"]["handler"](
        {"query": "庆大霉素 儿童 8 岁", "limit": 3}
    )
    assert search_result["success"] is True
    assert search_result["count"] >= 1
    top_rule_id = search_result["results"][0]["rule_id"]

    inspect_result = ctx.tools["rx_rule_inspect"]["handler"](
        {"rule_id": top_rule_id, "include_body": True}
    )
    assert inspect_result["success"] is True
    assert inspect_result["rule"]["rule_id"] == top_rule_id
    assert "body" in inspect_result


def test_apply_handler_rejects_missing_operator(sample_rules):
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)
    handler = ctx.tools["rx_rule_apply"]["handler"]
    result = handler(
        {
            "rule_id": "rx-aminoglycoside-pediatric",
            "order_context": {"drug_name": "庆大霉素", "patient_age": 5},
            "operator": "",
        }
    )
    assert result["success"] is False
    assert "operator" in result["error"]


def test_apply_handler_happy_path(sample_rules):
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)
    handler = ctx.tools["rx_rule_apply"]["handler"]
    result = handler(
        {
            "rule_id": "rx-aminoglycoside-pediatric",
            "order_context": {"drug_name": "庆大霉素", "patient_age": 5},
            "operator": "pharmacist-002",
        }
    )
    assert result["success"] is True
    applied = result["applied"]
    assert applied["confirmed"] is False
    assert applied["operator"] == "pharmacist-002"
    assert re.fullmatch(r"[0-9a-f]{64}", applied["order_hash"])


def test_search_handler_rejects_empty_query(sample_rules):
    bind_runtime(sample_rules)
    ctx = RecordingContext()
    register_rx_tools(ctx)
    handler = ctx.tools["rx_rule_search"]["handler"]
    result = handler({"query": ""})
    assert result["success"] is False
    assert "query" in result["error"]


# ---------------------------------------------------------------------------
# 6) re_split_sentences: 辅助分句
# ---------------------------------------------------------------------------


def test_re_split_sentences_basic():
    body = "氨基糖苷类药物在 8 岁以下儿童应严格限制使用。该类药物具有明显的耳毒性。"
    parts = re_split_sentences(body)
    assert "氨基糖苷类药物在 8 岁以下儿童应严格限制使用" in parts
    assert "该类药物具有明显的耳毒性" in parts


def test_re_split_sentences_empty():
    assert re_split_sentences("") == []


# ---------------------------------------------------------------------------
# 7) Task 19 · apply_rule 边界扩展
# 闭环维度:
#   - order_context 缺失或非 dict → ValueError
#   - operator 缺失或非字符串 → ValueError
#   - 相同 order_context JSON 序列化 → 相同 order_hash(幂等, sort_keys 保证字段顺序无关)
#   - session_id 由 operator + order_hash 派生(同输入同输出)
#   - audit log 强制写入 confirmed=False(默认,本工具永不代签)
# ---------------------------------------------------------------------------


def test_apply_rule_missing_order_context(sample_rules: list[dict[str, Any]]):
    """Missing (None) or empty ({}) or non-dict order_context → ValueError.

    与 ``test_apply_rule_missing_order_context_raises`` / ``_non_dict_order_context_raises``
    相比,这条更密集地覆盖 None / 空 dict / 非 dict(str / list / int) 4 类坏输入,
    保证 boundary contract 不被任何一类漏网。
    """
    # Empty dict → raise
    with pytest.raises(ValueError, match="order_context"):
        apply_rule("rx-aminoglycoside-pediatric", {}, sample_rules, operator="x")
    # None → raise(type: ignore: None 不是合法 order_context)
    with pytest.raises(ValueError, match="order_context"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            None,  # type: ignore[arg-type]
            sample_rules,
            operator="x",
        )
    # str → raise
    with pytest.raises(ValueError, match="order_context"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            "not a dict",  # type: ignore[arg-type]
            sample_rules,
            operator="x",
        )
    # list → raise
    with pytest.raises(ValueError, match="order_context"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            [{"drug_name": "庆大霉素"}],  # type: ignore[arg-type]
            sample_rules,
            operator="x",
        )
    # int → raise
    with pytest.raises(ValueError, match="order_context"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            42,  # type: ignore[arg-type]
            sample_rules,
            operator="x",
        )


def test_apply_rule_missing_operator(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """Missing (None / empty / whitespace) or non-str operator → ValueError."""
    # 空串 → raise
    with pytest.raises(ValueError, match="operator"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            sample_order_context,
            sample_rules,
            operator="",
        )
    # 纯空白 → raise(strip 后为空)
    with pytest.raises(ValueError, match="operator"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            sample_order_context,
            sample_rules,
            operator="   ",
        )
    # None → raise(type: ignore)
    with pytest.raises(ValueError, match="operator"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            sample_order_context,
            sample_rules,
            operator=None,  # type: ignore[arg-type]
        )
    # int → raise(type: ignore)
    with pytest.raises(ValueError, match="operator"):
        apply_rule(
            "rx-aminoglycoside-pediatric",
            sample_order_context,
            sample_rules,
            operator=42,  # type: ignore[arg-type]
        )


def test_apply_rule_idempotent_hash(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """同 order_context → 同 order_hash(sort_keys=True 保证字段顺序无关).

    覆盖:
      1) 直接重跑 → 同 hash
      2) 字段顺序交换 → 同 hash
      3) 嵌套 dict 字段顺序交换 → 同 hash(json.dumps sort_keys 递归)
      4) 嵌套 dict + 顶层字段顺序交换 → 同 hash
    """
    a = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    # 直接重跑
    c = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["order_hash"] == c["order_hash"]

    # 顶层字段顺序交换
    swapped = dict(reversed(list(sample_order_context.items())))
    b = apply_rule(
        "rx-aminoglycoside-pediatric",
        swapped,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["order_hash"] == b["order_hash"]

    # 嵌套 dict + 顶层字段顺序交换(json.dumps sort_keys=True 递归排序)
    nested_a = {
        "patient": {"age": 8, "weight_kg": 25},
        "drug": {"name": "庆大霉素", "dose_mg": 80},
    }
    nested_b = {
        "drug": {"dose_mg": 80, "name": "庆大霉素"},
        "patient": {"weight_kg": 25, "age": 8},
    }
    d = apply_rule(
        "rx-aminoglycoside-pediatric",
        nested_a,
        sample_rules,
        operator="pharmacist-001",
    )
    e = apply_rule(
        "rx-aminoglycoside-pediatric",
        nested_b,
        sample_rules,
        operator="pharmacist-001",
    )
    assert d["order_hash"] == e["order_hash"], (
        "json.dumps(sort_keys=True) 应递归排序, 但嵌套 dict 不同顺序得到了不同 hash"
    )


def test_apply_rule_idempotent_session(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """同 input(operator + order_context) → 同 session_id(幂等 for audit re-runs).

    session_id 由 sha256(f'{operator}|{order_hash}')[:16 hex] 派生;
    同 input 必须得到同一 session_id,便于审计回放与 HIS 端去重。
    """
    a = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    # 直接重跑
    b = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["session_id"] == b["session_id"]
    assert a["order_hash"] == b["order_hash"]

    # 字段顺序交换 → 因 sort_keys=True, order_hash 相同 → session_id 也相同
    swapped = dict(reversed(list(sample_order_context.items())))
    c = apply_rule(
        "rx-aminoglycoside-pediatric",
        swapped,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["session_id"] == c["session_id"], (
        "字段顺序不同的同语义 order_context 应派生同一 session_id"
    )


def test_apply_rule_audit_confirmed_false(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
    tmp_path: Path,
):
    """Audit log 行必须有 confirmed=False(本工具永不代签).

    覆盖多次 apply / 不同 operator / 不同 order_context 三种场景下
    audit.jsonl 中所有行的 ``confirmed`` 都必须严格为 False,
    守住 spec.md → ## 安全边界「apply 永不代签」契约。
    """
    audit_path = tmp_path / "audit" / "audit.jsonl"

    # 第一次调用:单行写入
    apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
        audit_path=audit_path,
    )
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["confirmed"] is False
    assert rows[0]["action"] == "apply"

    # 同一 input 重跑 → 第二行也是 confirmed=False
    apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
        audit_path=audit_path,
    )

    # 不同 operator 改写 → 第三行也是 confirmed=False
    apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-002",
        audit_path=audit_path,
    )

    # 不同 rule_id → 第四行也是 confirmed=False
    apply_rule(
        "rx-nsaid-pregnancy",
        {"drug_name": "布洛芬", "pregnancy": True},
        sample_rules,
        operator="pharmacist-001",
        audit_path=audit_path,
    )

    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    for idx, row in enumerate(rows):
        assert row["confirmed"] is False, (
            f"row {idx} 写入 audit 时 confirmed 应为 False, 实际: {row.get('confirmed')!r}"
        )
        assert row["action"] == "apply"


def test_apply_rule_different_operator_different_session(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """不同 operator → 不同 session_id, 但 order_hash 相同(prescription fingerprint 一致).

    审计追溯需要能区分「不同审方药师」「同一份处方」 vs 「同一药师 + 同处方重跑」,
    session_id 派生同时依赖 operator 与 order_hash,正好满足这一区分。
    """
    a = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    b = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-002",
    )
    assert a["session_id"] != b["session_id"], (
        "不同 operator 必须派生不同 session_id, 否则无法区分审方药师"
    )
    # 但 order_hash 相同(prescription fingerprint 不变)
    assert a["order_hash"] == b["order_hash"]


def test_apply_rule_whitespace_operator_stripped(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """operator 前后空白被 strip 后存入 payload + 用于 session_id 派生.

    避免因 caller 多打一个空格导致同一药师被错算成两个 session。
    """
    a = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="  pharmacist-001  ",
    )
    assert a["operator"] == "pharmacist-001", "payload.operator 应被 strip"
    b = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    assert a["session_id"] == b["session_id"], (
        "strip 后的 operator 应与无空白版本派生同一 session_id"
    )


def test_apply_rule_does_not_mutate_input(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
):
    """apply_rule 必须不修改 caller 传入的 order_context dict(避免隐蔽副作用)."""
    snapshot = json.dumps(sample_order_context, sort_keys=True, ensure_ascii=False)
    apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
    )
    after = json.dumps(sample_order_context, sort_keys=True, ensure_ascii=False)
    assert after == snapshot, "apply_rule 不应修改 caller's order_context"


def test_apply_rule_no_audit_when_path_none(
    sample_rules: list[dict[str, Any]],
    sample_order_context: dict[str, Any],
    tmp_path: Path,
):
    """audit_path=None 时不创建 audit 文件(便于 tests / dry-run)."""
    initial = set(tmp_path.iterdir())
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-001",
        audit_path=None,
    )
    assert isinstance(payload, dict)
    assert payload["confirmed"] is False
    assert set(tmp_path.iterdir()) == initial, (
        "audit_path=None 时不应在 tmp_path 创建任何文件"
    )


# ---------------------------------------------------------------------------
# Task 32: apply_rule 暴露 conflicts + recommended_rule_id
# ---------------------------------------------------------------------------


def test_apply_rule_exposes_conflicts_and_recommended_id(sample_rules):
    """apply_rule 返回 payload 必须包含 conflicts(list) 与 recommended_rule_id(str|None) 字段。

    即使 order_context 与真实 12 条规则仅产生 1 条命中,字段也必须稳定存在
    (空列表 + None),便于前端解析与审计追溯。
    """
    sample_order_context = {
        "patient_age": 8,
        "drug_name": "庆大霉素",
        "dose": "80mg",
        "route": "iv",
    }
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-test",
        audit_path=None,
    )
    # 字段必须存在且类型稳定
    assert "conflicts" in payload
    assert "recommended_rule_id" in payload
    assert isinstance(payload["conflicts"], list)
    # 既有字段不变(向后兼容 Task 8 / 19 的契约)
    assert payload["rule_id"] == "rx-aminoglycoside-pediatric"
    assert payload["confirmed"] is False
    assert payload["operator"] == "pharmacist-test"


def test_apply_rule_recommended_id_picked_when_same_class_high(sample_rules):
    """query 命中同 drug_class 多条 high 规则时,apply_rule 应给出 recommended_rule_id。"""
    # 构造触发同 drug_class_multiple_high 的 order_context:
    # 庆大霉素 + 儿童 → search 同时命中 rx-aminoglycoside-pediatric(high,儿科)
    # 与可能的其他 high 儿科规则。
    sample_order_context = {
        "patient_age": 8,
        "drug_name": "庆大霉素",
        "dose": "80mg",
        "route": "iv",
        "department": "儿科",
    }
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-recommend",
        audit_path=None,
    )
    # conflicts 列表应至少包含 1 条 same_drug_class_multiple_high
    # (若有另一条 high 儿科规则同时命中)
    type1 = [
        c for c in payload["conflicts"]
        if c["conflict_type"] == "same_drug_class_multiple_high"
    ]
    if type1:
        # 命中时:recommended_rule_id 应当被设上(优先取 specificity 最高的)
        assert payload["recommended_rule_id"] is not None
        # 该 id 应出现在冲突 rule_ids 中
        rec_id = payload["recommended_rule_id"]
        assert rec_id in type1[0]["rule_ids"]
    else:
        # 真实 12 条规则下若未触发,验证字段仍稳定为 None
        assert payload["recommended_rule_id"] is None
    # 字段类型稳定
    assert isinstance(payload["conflicts"], list)


def test_apply_rule_with_explicit_top_rules_respects_input(sample_rules):
    """apply_rule 接受显式 top_rules 参数时,应直接基于该输入跑冲突检测(不走 search)。"""
    # 构造两个 fake rule + 一个真实规则作为 top_rules 输入,
    # 验证 detect_rule_conflicts 在 apply 出口确实被调用。
    real_rule = next(
        r for r in sample_rules if r["rule_id"] == "rx-aminoglycoside-pediatric"
    )
    fake_a = {
        "rule_id": "rx-fake-applied-a",
        "drug_class": "儿科",
        "severity": "high",
        "population": "8 岁以下儿童",
        "body": "fake-a 内容",
        "applies_to": [{"age_max": 8}],
    }
    fake_b = {
        "rule_id": "rx-fake-applied-b",
        "drug_class": "儿科",
        "severity": "high",
        "population": "儿童",
        "body": "fake-b 内容很长很长" * 30,
        "applies_to": [{"age_max": 12}],
    }
    sample_order_context = {
        "patient_age": 8,
        "drug_name": "庆大霉素",
        "dose": "80mg",
    }
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-explicit",
        audit_path=None,
        top_rules=[(real_rule, 10.0), (fake_a, 9.0), (fake_b, 8.0)],
    )
    # 应触发同 drug_class 多 high 冲突(儿科 fake + 真实儿科 high)
    type1 = [
        c for c in payload["conflicts"]
        if c["conflict_type"] == "same_drug_class_multiple_high"
    ]
    assert len(type1) >= 1
    # recommended_rule_id 应指向 specificity 最高的规则
    rec_id = payload["recommended_rule_id"]
    assert rec_id is not None
    rule_ids = type1[0]["rule_ids"]
    assert rec_id in rule_ids


def test_apply_rule_contract_compatible_with_his_review_field(sample_rules):
    """apply_rule 返回字段完全对齐 contract.SCHEMA_HIS_REVIEW_FIELD(jsonschema 双向校验)。"""
    from jsonschema import Draft202012Validator
    from pass_field_check.contract import SCHEMA_HIS_REVIEW_FIELD

    sample_order_context = {
        "patient_age": 8,
        "drug_name": "庆大霉素",
        "dose": "80mg",
        "route": "iv",
    }
    payload = apply_rule(
        "rx-aminoglycoside-pediatric",
        sample_order_context,
        sample_rules,
        operator="pharmacist-contract",
        audit_path=None,
    )
    validator = Draft202012Validator(SCHEMA_HIS_REVIEW_FIELD)
    errors = list(validator.iter_errors(payload))
    assert not errors, f"payload 不符合 SCHEMA_HIS_REVIEW_FIELD: {errors}"
