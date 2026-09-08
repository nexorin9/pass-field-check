"""4 固定工具 schema + register_rx_tools。

源码产品能力参考：github_ref/agency-agents/scripts/build-hermes-plugin.py
  - SEARCH_SCHEMA / READ_SCHEMA / PROMPT_SCHEMA / DELEGATE_SCHEMA (L222-299)
    → 复用其 ``name / description / parameters{type,properties,required}``
    形态；改写为医院审方语义 4 工具 (rx_rule_search / inspect / load / apply)。
  - register(ctx) (L302-409)
    → 复用其 ``ctx.register_tool(name, toolset, schema, handler, description)``
    形态；改写为 ``register_rx_tools`` + 4 个 handler 闭包。
  - handler ``_json`` 返回 ``{success, ...}`` 字典再 json.dumps (L324-380)
    → 复用其"返回 JSON 字符串"的契约，但医院场景里我们直接返回 dict
    给上层 CLI/REST，由它们统一序列化（更易接入 Pydantic 校验）。

融合后的医院审方主路径：
  rx_rule_search(query) → top-N 适用规则
  → rx_rule_inspect(rule_id, include_body=True) → 规则全文与依据
  → rx_rule_load(rule_id, order_context) → 组装审方草稿（证据 + 处方上下文）
  → rx_rule_apply(rule_id, order_context, operator) → 审方意见 JSON
     {rule_id, order_hash, session_id, confirmed=False, applied_at, operator,
      evidence_excerpt} 落 audit.jsonl 由药师人工确认签字。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any

from .runtime import (
    _split_sentences_zh,
    _tokens_zh,
    detect_rule_conflicts,
    extract_evidence_excerpt,
    search_rules,
)


# ---------------------------------------------------------------------------
# 4 固定工具 schema 模板
# 复用 build-hermes-plugin.py:227-299 字段形态 (type=object / properties /
# required)，把 agent / slug / task / toolsets 等通用 agent 词汇改写为医院审方
# 词汇 (query / rule_id / order_context / operator / include_body)。
# ---------------------------------------------------------------------------

RX_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "rx_rule_search",
    "description": (
        "在院内用药规则目录中按 query 做 token-overlap 检索。"
        "适用：审方药师在工作站把用药医嘱字段串(药品名 + 剂量 + 给药途径 + "
        "人群/科室)丢进来，按 score + severity 命中 top-N 适用规则。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "用药医嘱字段串，例如 '庆大霉素 iv 80mg 8 岁'。"
                    "支持中英文混合，覆盖药品通用名 / 商品名 / ICD / 科室。"
                ),
            },
            "drug_class": {
                "type": "string",
                "description": (
                    "可选类目过滤，对应 rules_index.json 的 categories 之一 "
                    "(抗菌药 / 心血管 / 儿科 / 孕期 / 肾损)。"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "最大返回条数，默认 5。",
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["query"],
    },
}

RX_INSPECT_SCHEMA: dict[str, Any] = {
    "name": "rx_rule_inspect",
    "description": (
        "按 rule_id 取规则全文与依据片段。"
        "适用：审方药师在 search 命中后核对 body / applies_to 是否适用当前医嘱。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "规则 slug，例如 'rx-aminoglycoside-pediatric'。",
            },
            "include_body": {
                "type": "boolean",
                "description": "是否返回 body 全文。默认 True。",
            },
        },
        "required": ["rule_id"],
    },
}

RX_LOAD_SCHEMA: dict[str, Any] = {
    "name": "rx_rule_load",
    "description": (
        "按 rule_id 加载规则并组装审方草稿(含证据片段 + 处方上下文)，"
        "供审方药师人工复核并准备签字。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "规则 slug。",
            },
            "order_context": {
                "type": "object",
                "description": (
                    "用药医嘱字段上下文字典(patient_age / pregnancy / egfr / "
                    "drug_name / dose / route / frequency / department 等)。"
                ),
            },
        },
        "required": ["rule_id", "order_context"],
    },
}

RX_APPLY_SCHEMA: dict[str, Any] = {
    "name": "rx_rule_apply",
    "description": (
        "把审方意见草稿生成审方意见 JSON(rule_id + 证据 hash + session_id + "
        "operator + applied_at + confirmed=False)，进入 audit.jsonl 留痕。"
        "**重要：本工具永不自动签字**，confirmed 强制为 False，由审方药师"
        "在 HIS 审方栏人工确认后回写为 True。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "规则 slug。",
            },
            "order_context": {
                "type": "object",
                "description": "用药医嘱字段上下文字典。",
            },
            "operator": {
                "type": "string",
                "description": "审方药师工号 / 临床药师工号，强制必填。",
            },
        },
        "required": ["rule_id", "order_context", "operator"],
    },
}

# Public ordered tuple so register_rx_tools / tests can iterate without order bugs.
RX_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    RX_SEARCH_SCHEMA,
    RX_INSPECT_SCHEMA,
    RX_LOAD_SCHEMA,
    RX_APPLY_SCHEMA,
)


# ---------------------------------------------------------------------------
# 工具实现层：inspect_rule / load_rule / apply_rule
# 思路：复用 build-hermes-plugin.py:330-380 read/prompt/delegate handler
# 形态；改写为医院审方场景 inspect / load / apply。inspect_rule 与 load_rule
# 均为纯函数（便于 tests 直接 import），apply_rule 强校验 operator / order_context。
# ---------------------------------------------------------------------------


def _find_rule(rule_id: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Look up a rule by slug.  Raises ValueError if not found."""
    if not rule_id:
        raise ValueError("rule_id 必填")
    for rule in rules:
        if str(rule.get("rule_id", "")) == rule_id:
            return rule
    raise ValueError(f"rule_id 未找到: {rule_id!r}")


def inspect_rule(
    rule_id: str,
    rules: list[dict[str, Any]],
    *,
    include_body: bool = True,
) -> dict[str, Any]:
    """Return one rule record, optionally with its body.

    与 build-hermes-plugin.py:330-339 ``read`` handler 行为对齐：
    ``include_body`` 为 False 时只返回 summary 字段，方便 audit log
    减少冗余。
    """
    rule = _find_rule(rule_id, rules)
    summary = {
        "rule_id": rule.get("rule_id", ""),
        "drug_class": rule.get("drug_class", ""),
        "severity": rule.get("severity", ""),
        "evidence_source": rule.get("evidence_source", ""),
        "population": rule.get("population", ""),
        "applies_to": list(rule.get("applies_to", [])),
        "file_path": rule.get("file_path", ""),
    }
    payload: dict[str, Any] = {"success": True, "rule": summary}
    if include_body:
        payload["body"] = str(rule.get("body", ""))
    return payload


def _extract_evidence_excerpt(rule_body: str, order_context: dict[str, Any]) -> str:
    """Pick the most relevant sentence from the rule body for the given order.

    Thin wrapper around :func:`runtime.extract_evidence_excerpt`: tokenises
    the order_context values and delegates to the runtime helper so the
    selection logic lives in one place. Kept as a private shim so the
    load_rule / apply_rule call sites do not have to redo the tokenisation
    dance.
    """
    order_tokens: set[str] = set()
    for value in order_context.values():
        if isinstance(value, str):
            order_tokens |= _tokens_zh(value)
        elif isinstance(value, (int, float)):
            order_tokens |= _tokens_zh(str(value))

    return extract_evidence_excerpt(order_tokens, rule_body, max_chars=200)


# Backwards-compatible alias for the sentence splitter. The canonical
# implementation now lives in runtime.py (Task 18); this re-export keeps
# any third-party import path working.
def re_split_sentences(body: str) -> list[str]:
    """Deprecated alias for :func:`runtime._split_sentences_zh`.

    Kept here so existing imports (``from .tools import re_split_sentences``)
    still resolve; new code should import from ``runtime``.
    """
    return _split_sentences_zh(body)


def load_rule(
    rule_id: str,
    order_context: dict[str, Any],
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose a reviewer draft: rule summary + body excerpt + order context."""
    rule = _find_rule(rule_id, rules)
    summary = {
        "rule_id": rule.get("rule_id", ""),
        "drug_class": rule.get("drug_class", ""),
        "severity": rule.get("severity", ""),
        "evidence_source": rule.get("evidence_source", ""),
        "population": rule.get("population", ""),
        "applies_to": list(rule.get("applies_to", [])),
    }
    return {
        "success": True,
        "rule": summary,
        "order_context": dict(order_context),
        "evidence_excerpt": _extract_evidence_excerpt(
            str(rule.get("body", "")), dict(order_context)
        ),
    }


def _canonical_order_hash(order_context: dict[str, Any]) -> str:
    """sha256 over a canonical JSON serialisation (sort_keys=True).

    Field order independence lets the same logical prescription produce the
    same hash even when callers construct the dict in different orders.
    """
    payload = json.dumps(dict(order_context), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_id(operator: str, order_hash: str) -> str:
    """Derive a stable session id from operator + order_hash.

    Same input → same session id (idempotent for audit re-runs). 16 hex chars
    is short enough to paste into HIS 审方栏 comments.
    """
    seed = f"{operator}|{order_hash}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:16]


def apply_rule(
    rule_id: str,
    order_context: dict[str, Any],
    rules: list[dict[str, Any]],
    *,
    operator: str,
    audit_path: Path | None = None,
    top_rules: list[tuple[dict[str, Any], float]] | None = None,
) -> dict[str, Any]:
    """Build the review comment JSON for the audit log.

    Returns a dict (not JSON-encoded string) so the REST layer can wrap it in
    Pydantic models; ``confirmed`` is always ``False`` here -- the HIS 审方栏
    writes ``True`` after the pharmacist manually signs off.

    If ``audit_path`` is provided, the call also appends a JSONL audit row
    (still with ``confirmed=False``).

    Parameters
    ----------
    top_rules : list[tuple[rule, score]] | None
        Optional pre-computed search result set.  When provided (or when
        ``top_rules`` is recoverable via :func:`search_rules` over the same
        ``rules`` corpus + ``order_context``), :func:`detect_rule_conflicts`
        runs and the resulting ``conflicts`` + ``recommended_rule_id`` are
        attached to the returned payload so the pharmacist sees the
        "multiple high rules fire at once" hint inline with the review
        comment.  When ``top_rules`` is omitted the payload still includes
        ``conflicts=[]`` + ``recommended_rule_id=None`` for a stable schema.
    """
    if not isinstance(order_context, dict) or not order_context:
        raise ValueError("order_context 必填且非空")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("operator 必填(审方药师工号)")

    rule = _find_rule(rule_id, rules)
    order_hash = _canonical_order_hash(order_context)
    session_id = _session_id(operator.strip(), order_hash)
    applied_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    evidence_excerpt = _extract_evidence_excerpt(
        str(rule.get("body", "")), dict(order_context)
    )

    # ------------------------------------------------------------------
    # 规则互斥与多规则高亮 (Task 32):暴露 conflicts + recommended_rule_id
    # 供药师人工复核。若 caller 未传 top_rules,自动用 order_context 跑一次
    # search_rules 取 top-N(limit=5)作为分析输入,保证字段稳定出现。
    # ------------------------------------------------------------------
    if top_rules is None:
        query_text = " ".join(
            str(v)
            for v in order_context.values()
            if isinstance(v, (str, int, float))
        )
        raw_results = search_rules(query_text, rules, limit=5)
        top_rules = []
        # search_rules 返回 trimmed summary,我们需要完整 rule 记录(含 applies_to
        # / population / severity)才能跑冲突检测;按 rule_id 回查 rules 列表。
        results_by_id = {str(r.get("rule_id")): r for r in raw_results}
        top_rules = [
            (results_by_id[str(r.get("rule_id"))], r.get("score", 0.0))
            for r in raw_results
            if str(r.get("rule_id")) in results_by_id
        ]

    conflicts_result = detect_rule_conflicts(order_context, top_rules)

    payload: dict[str, Any] = {
        "rule_id": rule_id,
        "order_hash": order_hash,
        "session_id": session_id,
        "operator": operator.strip(),
        "applied_at": applied_at,
        "evidence_excerpt": evidence_excerpt,
        "confirmed": False,
        "conflicts": list(conflicts_result.get("conflicts", [])),
        "recommended_rule_id": conflicts_result.get("recommended_rule_id"),
    }

    if audit_path is not None:
        append_audit_event(audit_path, payload, rule=rule)

    return payload


# ---------------------------------------------------------------------------
# Audit append 帮手：apply_rule 默认写 audit.jsonl（与 task 11 audit.py 共享
# 字段形状；此处用纯 jsonl append 避免循环依赖，task 11 会用同一字段集读回。）
# ---------------------------------------------------------------------------


def append_audit_event(
    audit_path: Path,
    payload: dict[str, Any],
    *,
    rule: dict[str, Any] | None = None,
) -> None:
    """Append one audit row to audit.jsonl. Creates parent dirs as needed."""
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("timestamp", _dt.datetime.now(_dt.timezone.utc).isoformat())
    record.setdefault("action", "apply")
    if rule is not None:
        record.setdefault("rule_id", str(rule.get("rule_id", "")))
        record.setdefault("drug_class", str(rule.get("drug_class", "")))
        record.setdefault("severity", str(rule.get("severity", "")))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


# ---------------------------------------------------------------------------
# register_rx_tools: 4 handler 闭包
# 复用 build-hermes-plugin.py:302-409 ``register`` 模板：handler 接收 args 字典
# 返回 JSON 字符串；注册 4 工具名为 rx_rule_{search,inspect,load,apply}。
# 与源产品不同的是：本实现的 handler 返回 dict（不再 json.dumps），
# 由 CLI / REST 层统一序列化，避免重复 escape。
# ---------------------------------------------------------------------------


def register_rx_tools(ctx: Any) -> None:
    """Register the 4 reviewer-side tools on the given context.

    The ``ctx`` parameter is duck-typed: it must expose
    ``register_tool(name, toolset, schema, handler, description)`` (mirrors
    ``HermesPluginContext`` from the source product).  ``check-rx-rule-workbench``
    provides a ``RecordingContext`` shim for tests.
    """

    def search_handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"success": False, "error": "query is required"}
        drug_class = str(args.get("drug_class", "")).strip() or None
        try:
            limit = int(args.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        results = search_rules(query, _rules, drug_class=drug_class, limit=limit)
        return {"success": True, "query": query, "count": len(results), "results": results}

    def inspect_handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
        rule_id = str(args.get("rule_id", "")).strip()
        include_body = bool(args.get("include_body", True))
        try:
            return inspect_rule(rule_id, _rules, include_body=include_body)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    def load_handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
        rule_id = str(args.get("rule_id", "")).strip()
        order_context = args.get("order_context") or {}
        if not isinstance(order_context, dict):
            return {"success": False, "error": "order_context 必须是对象"}
        try:
            return load_rule(rule_id, order_context, _rules)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    def apply_handler(args: dict[str, Any], **_: Any) -> dict[str, Any]:
        rule_id = str(args.get("rule_id", "")).strip()
        order_context = args.get("order_context") or {}
        operator = str(args.get("operator", "")).strip()
        if not isinstance(order_context, dict):
            return {"success": False, "error": "order_context 必须是对象"}
        if not operator:
            return {"success": False, "error": "operator 必填(审方药师工号)"}
        try:
            payload = apply_rule(
                rule_id,
                order_context,
                _rules,
                operator=operator,
                audit_path=_audit_path,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "applied": payload}

    ctx.register_tool(
        name="rx_rule_search",
        toolset="rx_field_check",
        schema=RX_SEARCH_SCHEMA,
        handler=search_handler,
        description=RX_SEARCH_SCHEMA["description"],
    )
    ctx.register_tool(
        name="rx_rule_inspect",
        toolset="rx_field_check",
        schema=RX_INSPECT_SCHEMA,
        handler=inspect_handler,
        description=RX_INSPECT_SCHEMA["description"],
    )
    ctx.register_tool(
        name="rx_rule_load",
        toolset="rx_field_check",
        schema=RX_LOAD_SCHEMA,
        handler=load_handler,
        description=RX_LOAD_SCHEMA["description"],
    )
    ctx.register_tool(
        name="rx_rule_apply",
        toolset="rx_field_check",
        schema=RX_APPLY_SCHEMA,
        handler=apply_handler,
        description=RX_APPLY_SCHEMA["description"],
    )


# ---------------------------------------------------------------------------
# Context binding -- mirror build-hermes-plugin.py closure-over-module-state
# 写法。源产品用 closure 捕获 ``_load_agents()``；本实现捕获 ``_rules`` 与
# ``_audit_path`` 两个模块级变量，由 bind_runtime(rules, audit_path=None)
# 在 CLI / REST 启动时设置。
# ---------------------------------------------------------------------------


_rules: list[dict[str, Any]] = []
_audit_path: Path | None = None


def bind_runtime(rules: list[dict[str, Any]], audit_path: Path | None = None) -> None:
    """Bind the in-memory rules list and optional audit log path.

    Mirrors how the source product loads agents at registration time.  Called
    by ``cli.py`` and ``api.py`` during startup; tests can call directly.
    """
    global _rules, _audit_path
    _rules = list(rules)
    _audit_path = Path(audit_path) if audit_path is not None else None


def bound_rules() -> list[dict[str, Any]]:
    """Return the rules currently bound to the registry (for tests)."""
    return list(_rules)


__all__ = [
    "RX_SEARCH_SCHEMA",
    "RX_INSPECT_SCHEMA",
    "RX_LOAD_SCHEMA",
    "RX_APPLY_SCHEMA",
    "RX_TOOL_SCHEMAS",
    "register_rx_tools",
    "bind_runtime",
    "bound_rules",
    "inspect_rule",
    "load_rule",
    "apply_rule",
    "append_audit_event",
    "re_split_sentences",
]
