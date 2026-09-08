"""FastAPI REST 5 路由:HIS 审方栏对接面。

源码产品能力参考(参考地基 / evidence chain):
  github_ref/agency-agents/scripts/build-hermes-plugin.py
    - 启动期 bind_runtime(closure-over-module-state) → mirror 源产品写法;
      rules 与 audit_path 在 create_app() 注入,便于 tests 用 FastAPI
      TestClient 直接调,无需起 uvicorn 进程。
    - L227-299 4 工具 JSON Schema → 与 contract.py SCHEMA_*_REQUEST
      字段形态一致,便于 test_api.py 用 jsonschema 双向校验。
  github_ref/agency-agents/scripts/check-hermes-plugin.py
    - 退出码契约(0 = ok,非零 = fail) → REST 层映射为
      2xx vs 4xx/5xx,便于 HIS 厂家按 HTTP 标准对接。

融合后产品主路径(REST 形态):
  POST /search    {query, drug_class?, limit?}    → top-N 适用规则
  POST /inspect   {rule_id, include_body?}         → 规则全文 JSON
  POST /load      {rule_id, order_context}        → 审方草稿(规则+证据+处方)
  POST /apply     {rule_id, order_context, operator}
                    → SCHEMA_HIS_REVIEW_FIELD(confirmed=False)+ audit 留痕
  POST /audit/export {month, out_path?}
                    → CSV(utf-8-sig BOM,Excel 可直开)
  GET  /openapi.json / /docs(Swagger UI)/ /redoc(ReDoc)

响应统一格式:{ok: bool, data: ..., error: ...}(见 SCHEMA_RESPONSE_ENVELOPE)。
应用默认 host=127.0.0.1, port=8765(由环境变量 RX_API_HOST / RX_API_PORT 覆盖)。

mock 替身边界(spec.md ## 对接层):
  本 REST 面即为最终 HIS 审方栏对接格式;tests/ 中 mock_prescriptions /
  mock_his_fields 仅用于 pytest fixture,不作最终 integration surface。
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import tools
from .audit import query_events as audit_query_events
from .contract import (
    SCHEMA_APPLY_REQUEST,
    SCHEMA_APPLY_RESPONSE,
    SCHEMA_AUDIT_EVENT,
    SCHEMA_AUDIT_EXPORT_REQUEST,
    SCHEMA_AUDIT_EXPORT_RESPONSE,
    SCHEMA_AUDIT_QUERY_RESPONSE,
    SCHEMA_HIS_REVIEW_FIELD,
    SCHEMA_INSPECT_REQUEST,
    SCHEMA_INSPECT_RESPONSE,
    SCHEMA_LOAD_REQUEST,
    SCHEMA_LOAD_RESPONSE,
    SCHEMA_SEARCH_REQUEST,
    SCHEMA_SEARCH_RESPONSE,
    ErrorCode,
)
from .runtime import search_rules

# Default host / port — overridable via RX_API_HOST / RX_API_PORT at startup.
_DEFAULT_API_HOST = "127.0.0.1"
_DEFAULT_API_PORT = 8765

# Audit log path used by audit-export route before task 11 audit.py lands.
# tools.append_audit_event writes JSONL with timestamp / action / rule_id /
# drug_class / severity / order_hash / session_id / operator / confirmed;
# the audit-export endpoint reads the same shape.
_AUDIT_CSV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "rule_id",
    "action",
    "operator",
    "session_id",
    "order_hash",
    "confirmed",
)


# ---------------------------------------------------------------------------
# Pydantic models for request/response — mirror contract.py field shapes
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, description="用药医嘱字段串")
    drug_class: str | None = Field(default=None, description="可选类目过滤")
    limit: int = Field(default=5, ge=1, le=25, description="最大返回条数")


class SearchResult(BaseModel):
    rule_id: str
    drug_class: str
    severity: str
    evidence_source: str
    population: str
    file_path: str
    score: float


class SearchData(BaseModel):
    query: str
    count: int
    results: list[SearchResult]


class SearchResponse(BaseModel):
    ok: bool = True
    data: SearchData


class InspectRequest(BaseModel):
    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    include_body: bool = True


class InspectData(BaseModel):
    rule_id: str
    drug_class: str
    severity: str
    evidence_source: str
    population: str
    applies_to: list[str]
    file_path: str
    body: str | None = None


class InspectResponse(BaseModel):
    ok: bool
    data: InspectData | None = None


class LoadRequest(BaseModel):
    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    order_context: dict[str, Any]


class LoadData(BaseModel):
    rule: dict[str, Any]
    order_context: dict[str, Any]
    evidence_excerpt: str


class LoadResponse(BaseModel):
    ok: bool
    data: LoadData | None = None


class ApplyRequest(BaseModel):
    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    order_context: dict[str, Any] = Field(min_length=1)  # type: ignore[call-arg]
    operator: str = Field(min_length=1)

    @field_validator("order_context")
    @classmethod
    def _order_context_non_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("order_context 必填且非空")
        return value


class HisReviewField(BaseModel):
    """对齐 contract.SCHEMA_HIS_REVIEW_FIELD 的 Pydantic 镜像。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    order_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    operator: str
    applied_at: str
    evidence_excerpt: str
    confirmed: bool = False
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    recommended_rule_id: str | None = None


class ApplyData(BaseModel):
    applied: HisReviewField


class ApplyResponse(BaseModel):
    ok: bool
    data: ApplyData


class AuditExportRequest(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    out: str | None = None


class AuditExportData(BaseModel):
    month: str
    out_path: str
    events: int


class AuditExportResponse(BaseModel):
    ok: bool
    data: AuditExportData


class AuditEvent(BaseModel):
    """对齐 contract.SCHEMA_AUDIT_EVENT 的 Pydantic 镜像。"""

    model_config = ConfigDict(extra="allow")

    timestamp: str
    action: str
    rule_id: str | None = None
    order_hash: str | None = None
    session_id: str | None = None
    operator: str | None = None
    confirmed: bool | None = None
    scores_top_n: list[float] | None = None


class AuditQueryData(BaseModel):
    events: list[AuditEvent]
    total: int
    page: int
    page_size: int


class AuditQueryResponse(BaseModel):
    ok: bool = True
    data: AuditQueryData


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    ok: bool = False
    error: ErrorBody


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rules_or_400() -> list[dict[str, Any]]:
    """Return the rules currently bound, raising 400 if empty."""
    rules = tools.bound_rules()
    if not rules:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_INPUT",
                "message": (
                    "未加载规则索引;请在启动时传入 rules 列表"
                    "(create_app(rules=[...]))或先运行 index-build。"
                ),
            },
        )
    return rules


def _envelope(ok: bool, data: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a {ok, data, error} envelope response payload."""
    payload: dict[str, Any] = {"ok": ok}
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error
    return payload


def _http_exc_to_envelope(exc: HTTPException) -> dict[str, Any]:
    """Translate a FastAPI HTTPException into the standard error envelope."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return _envelope(False, error=detail)
    return _envelope(
        False,
        error={
            "code": "INVALID_INPUT",
            "message": str(detail) if detail is not None else exc.__class__.__name__,
        },
    )


# ---------------------------------------------------------------------------
# create_app factory
# ---------------------------------------------------------------------------


def create_app(
    rules: list[dict[str, Any]] | None = None,
    *,
    audit_path: Path | str | None = None,
) -> FastAPI:
    """Create a FastAPI app bound to the given rules list.

    ``rules`` populates the in-memory registry that backs all 5 routes
    (mirrors how cli.py calls ``bind_runtime``).  ``audit_path`` overrides
    the audit JSONL path; when None, defaults to ``./audit/audit.jsonl``
    relative to the working directory.
    """
    if rules is None:
        rules = []
    if audit_path is None:
        audit_path = Path("./audit/audit.jsonl")
    audit_path = Path(audit_path)

    tools.bind_runtime(rules, audit_path=audit_path)

    app = FastAPI(
        title="用药字段对照工作台 REST",
        description=(
            "院内合理用药规则 markdown → JSON 索引 → REST 审方对接。\n\n"
            "**对接层说明(mock 替身边界)**:本 API 即为最终 HIS 审方栏对接格式;"
            "tests/ 中的 mock_prescriptions / mock_his_fields 仅用于 pytest fixture,"
            "**不作最终 integration surface**。\n\n"
            "**安全边界**:confirmed 字段始终为 False(本工具永不代签);HIS 端"
            "审方药师人工确认后写回 True。\n\n"
            "**OpenAPI 文档**:`/docs`(Swagger UI)/ `/redoc`(ReDoc)/ "
            "`/openapi.json`(JSON 契约)三个端点便于 HIS 厂家按合同对接。"
        ),
        version="0.1.0",
        openapi_tags=[
            {
                "name": "审方",
                "description": (
                    "审方药师 / 临床药师在工作站调用:用药医嘱字段串检索 / "
                    "规则全文取阅 / 审方草稿组装 / 审方意见 JSON 生成。"
                ),
            },
            {
                "name": "审计",
                "description": (
                    "药事管理 / 临床药师月度简报:audit.jsonl 月度 CSV 导出"
                    "(utf-8-sig BOM,Excel 可直开)。"
                ),
            },
        ],
    )

    # -----------------------------------------------------------------------
    # Custom OpenAPI:补充 tags / contact / description,与公开 /openapi.json
    # 契约一致(HIS 厂家按 contract.py 对接)。
    # -----------------------------------------------------------------------

    def custom_openapi() -> dict[str, Any]:
        """自定义 OpenAPI schema:补充 contact / license / 内部门标识。

        触发条件:首次访问 ``/openapi.json`` 时 FastAPI 调用本函数生成
        并缓存 schema;后续访问直接返回缓存,避免重复构造。
        """
        if app.openapi_schema:
            return app.openapi_schema
        schema = FastAPI.openapi(app)
        schema["info"]["contact"] = {
            "name": "用药字段对照工作台",
            "x-internal-only": True,
            "description": (
                "内网辅助工具;REST 默认绑定 127.0.0.1:8765,不直接暴露公网。"
            ),
        }
        schema["info"]["license"] = {
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        }
        # OpenAPI 3.x: server 列表声明默认 host:port
        schema.setdefault("servers", []).append(
            {
                "url": f"http://{os.environ.get('RX_API_HOST', _DEFAULT_API_HOST)}:"
                f"{os.environ.get('RX_API_PORT', str(_DEFAULT_API_PORT))}",
                "description": "默认本地端口",
            }
        )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[assignment]

    # -----------------------------------------------------------------------
    # POST /search
    # -----------------------------------------------------------------------

    @app.post(
        "/search",
        response_model=SearchResponse,
        responses={400: {"model": ErrorResponse}},
        tags=["审方"],
        summary="按用药医嘱字段串命中 top-N 适用规则",
    )
    def search_endpoint(req: SearchRequest) -> dict[str, Any]:
        rules_list = _rules_or_400()
        # Handler-level validation:reject whitespace-only / empty query that
        # Pydantic min_length=1 already filters when "" is passed. This branch
        # catches e.g. "   " (single space) which Pydantic considers valid
        # (length=3) but the tokenizer would otherwise return zero hits.
        if not req.query or not req.query.strip():
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_INPUT",
                    "message": "query 必填且非空白",
                },
            )
        results = search_rules(
            req.query,
            rules_list,
            drug_class=req.drug_class,
            limit=req.limit,
        )
        return _envelope(
            True,
            data={
                "query": req.query,
                "count": len(results),
                "results": results,
            },
        )

    # -----------------------------------------------------------------------
    # POST /inspect
    # -----------------------------------------------------------------------

    @app.post(
        "/inspect",
        response_model=InspectResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["审方"],
        summary="按 rule_id 取规则全文",
    )
    def inspect_endpoint(req: InspectRequest) -> dict[str, Any]:
        rules_list = _rules_or_400()
        try:
            result = tools.inspect_rule(
                req.rule_id, rules_list, include_body=req.include_body
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RULE_NOT_FOUND", "message": str(exc)},
            ) from exc
        if not result.get("success"):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RULE_NOT_FOUND",
                    "message": result.get("error", "unknown"),
                },
            )
        rule = result["rule"]
        payload = dict(rule)
        payload["body"] = result.get("body", "") if req.include_body else ""
        return _envelope(True, data=payload)

    # -----------------------------------------------------------------------
    # POST /load
    # -----------------------------------------------------------------------

    @app.post(
        "/load",
        response_model=LoadResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
        tags=["审方"],
        summary="组装审方草稿(规则 + 处方 + 证据片段)",
    )
    def load_endpoint(req: LoadRequest) -> dict[str, Any]:
        rules_list = _rules_or_400()
        try:
            result = tools.load_rule(req.rule_id, req.order_context, rules_list)
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "RULE_NOT_FOUND", "message": str(exc)},
            ) from exc
        return _envelope(
            True,
            data={
                "rule": result["rule"],
                "order_context": result["order_context"],
                "evidence_excerpt": result["evidence_excerpt"],
            },
        )

    # -----------------------------------------------------------------------
    # POST /apply
    # -----------------------------------------------------------------------

    @app.post(
        "/apply",
        response_model=ApplyResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
        },
        tags=["审方"],
        summary=(
            "生成 HIS 审方栏对接字段 + audit.jsonl 留痕"
            "(confirmed 强制 False)"
        ),
    )
    def apply_endpoint(req: ApplyRequest) -> dict[str, Any]:
        rules_list = _rules_or_400()
        try:
            payload = tools.apply_rule(
                req.rule_id,
                req.order_context,
                rules_list,
                operator=req.operator,
                audit_path=audit_path,
            )
        except ValueError as exc:
            msg = str(exc)
            code = "RULE_NOT_FOUND" if "未找到" in msg else "INVALID_INPUT"
            status = 404 if code == "RULE_NOT_FOUND" else 400
            raise HTTPException(
                status_code=status,
                detail={"code": code, "message": msg},
            ) from exc
        return _envelope(True, data={"applied": payload})

    # -----------------------------------------------------------------------
    # POST /audit/export
    # -----------------------------------------------------------------------

    @app.post(
        "/audit/export",
        response_model=AuditExportResponse,
        responses={400: {"model": ErrorResponse}},
        tags=["审计"],
        summary="月度 audit.jsonl 导出 CSV(utf-8-sig BOM,Excel 可直开)",
    )
    def audit_export_endpoint(req: AuditExportRequest) -> dict[str, Any]:
        if not audit_path.exists():
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_INPUT",
                    "message": f"audit log 不存在: {audit_path}",
                },
            )
        events: list[dict[str, Any]] = []
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue

        month_events = [
            ev
            for ev in events
            if str(ev.get("timestamp", "")).startswith(req.month)
        ]

        if req.out:
            out_path = Path(req.out).resolve()
        else:
            out_path = audit_path.with_name(f"audit-{req.month}.csv").resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(_AUDIT_CSV_COLUMNS)
            for ev in month_events:
                writer.writerow([ev.get(col, "") for col in _AUDIT_CSV_COLUMNS])

        return _envelope(
            True,
            data={
                "month": req.month,
                "out_path": str(out_path),
                "events": len(month_events),
            },
        )

    # -----------------------------------------------------------------------
    # GET /audit/events — 检索 / 过滤 / 分页
    # query params:operator / rule_id / start_date / end_date / action /
    #               page / page_size
    # -----------------------------------------------------------------------

    from fastapi import Query as _Query  # local import:avoid clashing tests

    @app.get(
        "/audit/events",
        response_model=AuditQueryResponse,
        tags=["审计"],
        summary="按 operator / rule_id / 时间段 / action 多条件查询 + 分页",
    )
    def audit_events_endpoint(
        operator: str | None = _Query(
            default=None,
            description="审方药师工号(严格相等过滤)",
        ),
        rule_id: str | None = _Query(
            default=None,
            description="命中规则 slug(严格相等)",
            pattern=r"^[a-z0-9][a-z0-9-]*$",
        ),
        start_date: str | None = _Query(
            default=None,
            description="起始 ISO 8601(包含);半开区间 [start_date, end_date)",
        ),
        end_date: str | None = _Query(
            default=None,
            description="结束 ISO 8601(不包含);半开区间 [start_date, end_date)",
        ),
        action: str | None = _Query(
            default=None,
            description="事件动作类型(严格相等):search/inspect/load/apply",
            pattern=r"^(search|inspect|load|apply)$",
        ),
        page: int = _Query(default=1, ge=1, description="页码(1-based)"),
        page_size: int = _Query(
            default=50, ge=1, le=500, description="每页事件数(已 clamp 到 ≤500)"
        ),
    ) -> dict[str, Any]:
        """按多条件过滤 audit.jsonl,分页返回。audit 文件不存在时返回 200 + 空列表。"""
        # 早失败:start_date / end_date 格式校验交由 audit_query_events
        # 内部 _parse_iso8601 失败时该事件被排除;但为提供 4xx 友好提示,
        # 显式校验一次:若任一非空但 parse 失败 → 400 INVALID_INPUT。
        from .audit import _parse_iso8601 as _audit_parse_iso8601  # local

        for label, value in (("start_date", start_date), ("end_date", end_date)):
            if value and _audit_parse_iso8601(value) is None:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INVALID_INPUT",
                        "message": (
                            f"{label} 必须是 ISO 8601 时间串(包含时区),"
                            f"如 '2026-09-01T00:00:00+00:00';收到: {value!r}"
                        ),
                    },
                )
        if (
            start_date
            and end_date
            and _audit_parse_iso8601(start_date) is not None
            and _audit_parse_iso8601(end_date) is not None
            and _audit_parse_iso8601(start_date) > _audit_parse_iso8601(end_date)
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_INPUT",
                    "message": "start_date 必须 ≤ end_date",
                },
            )

        result = audit_query_events(
            audit_path,
            operator=operator,
            rule_id=rule_id,
            start=start_date,
            end=end_date,
            action=action,
            page=page,
            page_size=page_size,
        )
        return _envelope(
            True,
            data={
                "events": result["events"],
                "total": result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            },
        )

    # -----------------------------------------------------------------------
    # Exception handlers — translate HTTPException into the standard envelope.
    # -----------------------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(_request: Any, exc: HTTPException) -> JSONResponse:
        # Re-shape the HTTPException body into our envelope. Returning a
        # JSONResponse (not a raw dict) is required so FastAPI can keep the
        # original status_code on the wire.
        return JSONResponse(
            status_code=exc.status_code,
            content=_http_exc_to_envelope(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(
        _request: Any, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic 模型校验失败 → 422 + INVALID_INPUT 包装。
        # Pydantic 默认 422 走 {"detail": [...]} 不符合 SCHEMA_ERROR_RESPONSE;
        # 本处理器统一为 {ok: False, error: {code, message, detail}}。
        return JSONResponse(
            status_code=422,
            content=_envelope(
                False,
                error={
                    "code": "INVALID_INPUT",
                    "message": "Pydantic 模型校验失败",
                    "detail": exc.errors(),
                },
            ),
        )

    @app.exception_handler(Exception)
    async def _internal_exc_handler(_request: Any, exc: Exception) -> JSONResponse:
        # 兜底:任何未捕获异常 → 500 + INTERNAL_ERROR。
        # 真实业务状态不得因此被悄悄推进;仅记录错误信息。
        return JSONResponse(
            status_code=500,
            content=_envelope(
                False,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": str(exc) or exc.__class__.__name__,
                    "detail": None,
                },
            ),
        )

    return app


# Module-level app for `uvicorn pass_field_check.api:app`.
# Reads RX_API_HOST / RX_API_PORT from env so a single uvicorn process picks
# up the configured host/port.  When no rules are available the API returns
# 400 INVALID_INPUT on /search etc.; index-build is expected to populate
# rules/audit before serving live traffic.
_app_default_rules: list[dict[str, Any]] = []
try:  # pragma: no cover — best-effort default wiring
    from .index_builder import collect_rx_rules as _collect_default

    _default_rules_dir = Path(os.environ.get("RX_RULES_DIR", "./rules"))
    if _default_rules_dir.is_dir():
        try:
            _app_default_rules = _collect_default(_default_rules_dir)
        except (ValueError, OSError):
            _app_default_rules = []
except Exception:  # pragma: no cover — defensive default
    _app_default_rules = []


def _resolve_default_audit_path() -> Path:
    env_path = os.environ.get("RX_AUDIT_PATH")
    if env_path:
        return Path(env_path)
    return Path("./audit/audit.jsonl")


app = create_app(
    rules=_app_default_rules,
    audit_path=_resolve_default_audit_path(),
)


def main() -> int:
    """Entry point for `uvicorn pass_field_check.api:app --host ... --port ...`.

    Reads RX_API_HOST / RX_API_PORT environment variables (defaults
    127.0.0.1:8765 per task 10 spec).
    """
    host = os.environ.get("RX_API_HOST", _DEFAULT_API_HOST)
    try:
        port = int(os.environ.get("RX_API_PORT", str(_DEFAULT_API_PORT)))
    except ValueError:
        port = _DEFAULT_API_PORT

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("error: uvicorn 未安装;pip install 'uvicorn[standard]'\n")
        return 2

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


__all__ = ["app", "create_app", "main"]


# Force-references so static analysers see the contract schemas being used
# (the schemas live in contract.py and are exposed via OpenAPI for HIS 厂家对接).
_ = (
    SCHEMA_HIS_REVIEW_FIELD,
    SCHEMA_SEARCH_REQUEST,
    SCHEMA_SEARCH_RESPONSE,
    SCHEMA_INSPECT_REQUEST,
    SCHEMA_INSPECT_RESPONSE,
    SCHEMA_LOAD_REQUEST,
    SCHEMA_LOAD_RESPONSE,
    SCHEMA_APPLY_REQUEST,
    SCHEMA_APPLY_RESPONSE,
    SCHEMA_AUDIT_EXPORT_REQUEST,
    SCHEMA_AUDIT_EXPORT_RESPONSE,
    SCHEMA_AUDIT_EVENT,
    SCHEMA_AUDIT_QUERY_RESPONSE,
)