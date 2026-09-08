"""HIS 审方栏字段对接格式 + REST 请求/响应 Schema。

源码产品能力参考(参考地基 / evidence chain):
  github_ref/agency-agents/scripts/build-hermes-plugin.py
    - L222-299 4 工具 JSON Schema(type=object / properties / required)
      → 复用其字段形态,在 contract.py 中映射为 HIS 审方栏对接的
      SCHEMA_HIS_REVIEW_FIELD / SEARCH / INSPECT / LOAD / APPLY / AUDIT 字段。
  github_ref/agency-agents/scripts/check-hermes-plugin.py
    - RecordingContext 接收 schema 并校验入参 → 与本模块
      SCHEMA_*_REQUEST 一致(便于 test_api.py 用 jsonschema 直接验证)。

融合后产品主路径(医院 HIS 审方栏对接):
  HIS 审方栏 字段串 → POST /search
    → 入参 SCHEMA_SEARCH_REQUEST{query, drug_class, limit}
    → 出参 SCHEMA_SEARCH_RESPONSE{ok, data:{results:[{rule_id, drug_class,
       severity, evidence_source, population, file_path, score}]}}
  命中规则 → POST /apply
    → 入参 SCHEMA_APPLY_REQUEST{rule_id, order_context, operator}
    → 出参 SCHEMA_APPLY_RESPONSE{ok, data:{applied:SCHEMA_HIS_REVIEW_FIELD}}
    → audit.jsonl 留痕(confirmed=False 由药师人工回写为 True)

mock 替身边界(spec.md ## 对接层 显式声明):
  本模块定义的字段为正式 HIS 审方栏对接格式。tests/ 中 mock_prescriptions
  / mock_his_fields 仅用于 pytest,不作最终 integration surface。
"""
from __future__ import annotations

from typing import Any, Literal

# ---------------------------------------------------------------------------
# 顶层响应包装:统一 {ok, data, error} 形态
# ---------------------------------------------------------------------------

SCHEMA_RESPONSE_ENVELOPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {"type": ["object", "array", "string", "number", "null"]},
        "error": {
            "type": ["object", "null"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "detail": {"type": ["string", "object", "array", "null"]},
            },
        },
    },
    "required": ["ok"],
}


# ---------------------------------------------------------------------------
# SCHEMA_HIS_REVIEW_FIELD:HIS 审方栏正式对接字段
# (rule_id / order_hash / session_id / operator / applied_at /
#  evidence_excerpt / confirmed)
# 字段语义:
#   - rule_id:命中规则 slug(用于回查 rules_index.json)
#   - order_hash:sha256(canonical JSON(order_context)),字段顺序无关
#   - session_id:operator + order_hash 派生的 16 hex,幂等
#   - operator:审方药师工号(强制必填,用于审计追溯)
#   - applied_at:ISO 8601 with timezone,apply_rule 落盘时刻
#   - evidence_excerpt:从规则 body 抽取的相关片段(≤200 字,用于审方栏说明)
#   - confirmed:HIS 端写回 True 前始终 False;本工具永不代签
# ---------------------------------------------------------------------------

SCHEMA_HIS_REVIEW_FIELD: dict[str, Any] = {
    "type": "object",
    "description": (
        "HIS 审方栏正式对接字段——审方药师 apply 工具返回的审方意见 JSON。"
        "confirmed 强制为 False,本工具永不代签;HIS 端人工确认后写回 True。"
        "conflicts 与 recommended_rule_id 来自规则互斥检测(Task 32),"
        "同一 query 命中多条规则时给药师人工复核提示与推荐。"
    ),
    "properties": {
        "rule_id": {
            "type": "string",
            "description": "命中规则 slug,如 'rx-aminoglycoside-pediatric'。",
            "pattern": "^[a-z0-9][a-z0-9-]*$",
        },
        "order_hash": {
            "type": "string",
            "description": "sha256(canonical JSON(order_context)) → 64 hex。",
            "pattern": "^[a-f0-9]{64}$",
        },
        "session_id": {
            "type": "string",
            "description": "sha256(operator|order_hash)[:16 hex],幂等派生。",
            "pattern": "^[a-f0-9]{16}$",
        },
        "operator": {
            "type": "string",
            "description": "审方药师工号 / 临床药师工号,apply 强制必填。",
            "minLength": 1,
        },
        "applied_at": {
            "type": "string",
            "description": "ISO 8601 with timezone,apply_rule 落盘时刻。",
        },
        "evidence_excerpt": {
            "type": "string",
            "description": "从规则 body 抽取的 token-overlap 最高相关句,≤200 字。",
            "maxLength": 200,
        },
        "confirmed": {
            "type": "boolean",
            "description": "本工具输出永远为 False;HIS 端人工确认后写回 True。",
            "enum": [False],
        },
        "conflicts": {
            "type": "array",
            "description": (
                "规则互斥检测结果(Task 32):同一 query 命中多条规则时"
                "暴露的冲突条目;无冲突时为空列表。每条冲突含 conflict_type"
                " (same_drug_class_multiple_high / contradictory_applies_to /"
                " cross_drug_class_aggregation) / rule_ids / message 等字段。"
            ),
            "items": {"type": "object"},
            "default": [],
        },
        "recommended_rule_id": {
            "type": ["string", "null"],
            "description": (
                "互斥检测给出的推荐规则 slug(仅 same_drug_class_multiple_high"
                " 且能打分时返回);其余场景或无推荐时为 None。"
            ),
            "pattern": "^[a-z0-9][a-z0-9-]*$",
        },
    },
    "required": [
        "rule_id",
        "order_hash",
        "session_id",
        "operator",
        "applied_at",
        "evidence_excerpt",
        "confirmed",
        "conflicts",
        "recommended_rule_id",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# POST /search Request / Response
# ---------------------------------------------------------------------------

SCHEMA_SEARCH_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "POST /search 入参:用药医嘱字段串 + 可选类目/上限。",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": (
                "用药医嘱字段串,如 '庆大霉素 iv 80mg 8 岁';"
                "支持中英文混合,覆盖药品通用名 / 商品名 / ICD / 科室。"
            ),
        },
        "drug_class": {
            "type": "string",
            "description": (
                "可选类目过滤,对应 rules_index.json 的 categories "
                "(抗菌药 / 心血管 / 儿科 / 孕期 / 肾损)。"
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 25,
            "description": "最大返回条数,默认 5。",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


SCHEMA_SEARCH_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "POST /search 出参:top-N 适用规则 trimmed summary。",
    "properties": {
        "ok": {"type": "boolean", "enum": [True]},
        "data": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule_id": {"type": "string"},
                            "drug_class": {"type": "string"},
                            "severity": {"type": "string"},
                            "evidence_source": {"type": "string"},
                            "population": {"type": "string"},
                            "file_path": {"type": "string"},
                            "score": {"type": "number"},
                        },
                        "required": ["rule_id", "score"],
                    },
                },
            },
            "required": ["query", "count", "results"],
        },
    },
    "required": ["ok", "data"],
}


# ---------------------------------------------------------------------------
# POST /inspect Request / Response
# ---------------------------------------------------------------------------

SCHEMA_INSPECT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "POST /inspect 入参:rule_id + include_body 开关。",
    "properties": {
        "rule_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "include_body": {"type": "boolean", "default": True},
    },
    "required": ["rule_id"],
    "additionalProperties": False,
}


SCHEMA_INSPECT_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "drug_class": {"type": "string"},
                "severity": {"type": "string"},
                "evidence_source": {"type": "string"},
                "population": {"type": "string"},
                "applies_to": {"type": "array", "items": {"type": "string"}},
                "file_path": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    },
    "required": ["ok"],
}


# ---------------------------------------------------------------------------
# POST /load Request / Response(组装审方草稿,不含 audit 留痕)
# ---------------------------------------------------------------------------

SCHEMA_LOAD_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "POST /load 入参:rule_id + order_context 字典。",
    "properties": {
        "rule_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "order_context": {
            "type": "object",
            "description": (
                "用药医嘱字段上下文字典(patient_age / pregnancy / egfr / "
                "drug_name / dose / route / frequency / department 等)。"
            ),
        },
    },
    "required": ["rule_id", "order_context"],
    "additionalProperties": False,
}


SCHEMA_LOAD_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {
            "type": "object",
            "properties": {
                "rule": {
                    "type": "object",
                    "properties": {
                        "rule_id": {"type": "string"},
                        "drug_class": {"type": "string"},
                        "severity": {"type": "string"},
                        "evidence_source": {"type": "string"},
                        "population": {"type": "string"},
                        "applies_to": {"type": "array"},
                    },
                },
                "order_context": {"type": "object"},
                "evidence_excerpt": {"type": "string", "maxLength": 200},
            },
            "required": ["rule", "order_context", "evidence_excerpt"],
        },
    },
    "required": ["ok"],
}


# ---------------------------------------------------------------------------
# POST /apply Request / Response(生成 HIS 审方栏对接字段 + audit 留痕)
# ---------------------------------------------------------------------------

SCHEMA_APPLY_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "POST /apply 入参:rule_id + order_context + operator。"
        "operator 强制必填,用于审计追溯;HIS 端工号通常为审方药师 / "
        "临床药师工号。"
    ),
    "properties": {
        "rule_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "order_context": {
            "type": "object",
            "minProperties": 1,
            "description": "用药医嘱字段上下文字典(必填且非空)。",
        },
        "operator": {
            "type": "string",
            "minLength": 1,
            "description": "审方药师工号 / 临床药师工号。",
        },
    },
    "required": ["rule_id", "order_context", "operator"],
    "additionalProperties": False,
}


SCHEMA_APPLY_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "POST /apply 出参:applied 字段对齐 SCHEMA_HIS_REVIEW_FIELD。",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {
            "type": "object",
            "properties": {
                "applied": SCHEMA_HIS_REVIEW_FIELD,
            },
            "required": ["applied"],
        },
    },
    "required": ["ok", "data"],
}


# ---------------------------------------------------------------------------
# POST /audit/export Request / Response(月度 audit.jsonl → CSV 导出)
# ---------------------------------------------------------------------------

SCHEMA_AUDIT_EXPORT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "POST /audit/export 入参:month(YYYY-MM)。",
    "properties": {
        "month": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}$",
            "description": "月份,YYYY-MM。",
        },
        "out": {
            "type": "string",
            "description": "输出 CSV 路径(可选,默认 audit-{month}.csv)。",
        },
    },
    "required": ["month"],
    "additionalProperties": False,
}


SCHEMA_AUDIT_EXPORT_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {
            "type": "object",
            "properties": {
                "month": {"type": "string"},
                "out_path": {"type": "string"},
                "events": {"type": "integer"},
            },
            "required": ["month", "out_path", "events"],
        },
    },
    "required": ["ok", "data"],
}


# ---------------------------------------------------------------------------
# GET /audit/events 入参 + 出参 schema
# 字段语义:
#   - SCHEMA_AUDIT_EVENT 单条审计事件;必含 timestamp / rule_id / order_hash /
#     session_id / operator / action / confirmed,与 audit.jsonl append 行
#     字段对齐,便于 frontend 直接渲染。
#   - SCHEMA_AUDIT_QUERY_RESPONSE 出参:{events: [...], total, page, page_size}
#     page_size 自动 clamp 到 [1, 500];page 1-based。
# ---------------------------------------------------------------------------

SCHEMA_AUDIT_EVENT: dict[str, Any] = {
    "type": "object",
    "description": (
        "单条 audit 事件;字段与 audit.jsonl append 行为准,便于 frontend "
        "直渲染。scores_top_n 是 search 阶段缓存的 top-N 命中分(可能缺)。"
    ),
    "properties": {
        "timestamp": {
            "type": "string",
            "description": "ISO 8601 UTC,事件落盘时刻。",
        },
        "rule_id": {
            "type": ["string", "null"],
            "description": "命中规则 slug。",
            "pattern": "^[a-z0-9][a-z0-9-]*$",
        },
        "order_hash": {
            "type": ["string", "null"],
            "description": "sha256(canonical JSON(order_context))。",
            "pattern": "^[a-f0-9]{64}$",
        },
        "session_id": {
            "type": ["string", "null"],
            "description": "sha256(operator|order_hash)[:16 hex]。",
            "pattern": "^[a-f0-9]{16}$",
        },
        "operator": {
            "type": ["string", "null"],
            "description": "审方药师 / 临床药师工号。",
        },
        "action": {
            "type": "string",
            "enum": ["search", "inspect", "load", "apply"],
            "description": "事件动作类型。",
        },
        "confirmed": {
            "type": ["boolean", "null"],
            "description": "本工具输出 False;HIS 端人工确认后回写 True。",
        },
        "scores_top_n": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "description": "search 阶段 top-N 命中分(可能为空)。",
        },
    },
    "required": ["timestamp", "action"],
}


SCHEMA_AUDIT_QUERY_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "GET /audit/events 出参:分页 + 多条件过滤结果。",
    "properties": {
        "ok": {"type": "boolean", "enum": [True]},
        "data": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": SCHEMA_AUDIT_EVENT,
                },
                "total": {
                    "type": "integer",
                    "description": "过滤后总事件数(分页前)。",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "当前页码(1-based)。",
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "每页最大事件数(已 clamp)。",
                },
            },
            "required": ["events", "total", "page", "page_size"],
        },
    },
    "required": ["ok", "data"],
}


# ---------------------------------------------------------------------------
# SCHEMA_ERROR_RESPONSE:统一错误响应包装(便于 4xx/5xx)
# 字段语义:
#   - ok: 始终 False
#   - error.code:枚举(INVALID_INPUT / RULE_NOT_FOUND / INTERNAL_ERROR)
#   - error.message:人类可读错误描述
#   - error.detail:结构化细节(可空)
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    "INVALID_INPUT",
    "RULE_NOT_FOUND",
    "RULE_DUPLICATE",
    "INTERNAL_ERROR",
]

SCHEMA_ERROR_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "统一错误响应包装:所有 4xx/5xx 走此格式。",
    "properties": {
        "ok": {"type": "boolean", "enum": [False]},
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "detail": {"type": ["string", "object", "array", "null"]},
            },
            "required": ["code", "message"],
        },
    },
    "required": ["ok", "error"],
}


__all__ = [
    "SCHEMA_RESPONSE_ENVELOPE",
    "SCHEMA_HIS_REVIEW_FIELD",
    "SCHEMA_SEARCH_REQUEST",
    "SCHEMA_SEARCH_RESPONSE",
    "SCHEMA_INSPECT_REQUEST",
    "SCHEMA_INSPECT_RESPONSE",
    "SCHEMA_LOAD_REQUEST",
    "SCHEMA_LOAD_RESPONSE",
    "SCHEMA_APPLY_REQUEST",
    "SCHEMA_APPLY_RESPONSE",
    "SCHEMA_AUDIT_EXPORT_REQUEST",
    "SCHEMA_AUDIT_EXPORT_RESPONSE",
    "SCHEMA_AUDIT_EVENT",
    "SCHEMA_AUDIT_QUERY_RESPONSE",
    "SCHEMA_ERROR_RESPONSE",
    "ErrorCode",
]