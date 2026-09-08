"""tests/test_api.py:FastAPI REST 5 路由契约测试 + OpenAPI 文档测试。

源码产品能力参考(参考地基 / evidence chain):
  github_ref/agency-agents/scripts/build-hermes-plugin.py
    - L227-299 4 工具 JSON Schema 字段形态 → 与 contract.py SCHEMA_*_REQUEST
      一致;test_api.py 用 jsonschema 双向校验请求/响应 payload,守住字段契约。
    - bind_runtime(closure-over-module-state) → create_app() 注入 rules +
      audit_path,tests 不需要真实 uvicorn 进程,FastAPI TestClient 即可。
  github_ref/agency-agents/scripts/check-hermes-plugin.py
    - check-rx-rule-workbench 的 smoke 烟测 → test_api.py 中
      test_search_to_inspect_chain 沿用 search → inspect 链路烟测。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from pass_field_check.api import create_app
from pass_field_check.contract import (
    SCHEMA_APPLY_REQUEST,
    SCHEMA_APPLY_RESPONSE,
    SCHEMA_AUDIT_EXPORT_REQUEST,
    SCHEMA_AUDIT_EXPORT_RESPONSE,
    SCHEMA_HIS_REVIEW_FIELD,
    SCHEMA_INSPECT_REQUEST,
    SCHEMA_LOAD_REQUEST,
    SCHEMA_LOAD_RESPONSE,
    SCHEMA_SEARCH_REQUEST,
    SCHEMA_SEARCH_RESPONSE,
)
from pass_field_check.index_builder import collect_rx_rules
from pass_field_check import tools as tools_module

# ---------------------------------------------------------------------------
# Fixtures: load real rules corpus + isolated audit dir per test
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / "rules"


@pytest.fixture(scope="module")
def rules_list() -> list[dict]:
    """Real rules corpus from rules/ directory (≥12 rules)."""
    if not RULES_DIR.is_dir():
        pytest.skip(f"rules 目录不存在: {RULES_DIR}")
    return collect_rx_rules(RULES_DIR)


@pytest.fixture()
def audit_dir(tmp_path: Path) -> Path:
    """Per-test isolated audit directory."""
    audit = tmp_path / "audit"
    audit.mkdir()
    return audit


@pytest.fixture()
def app_and_client(rules_list: list[dict], audit_dir: Path):
    """Create a TestClient bound to real rules + isolated audit_path."""
    audit_path = audit_dir / "audit.jsonl"
    application = create_app(rules=rules_list, audit_path=audit_path)
    client = TestClient(application)
    yield application, client, audit_path
    # Restore module-level bindings to avoid leaking between tests
    tools_module.bind_runtime([])


# ---------------------------------------------------------------------------
# 1) POST /search — token-overlap top-N + 类目过滤 + 字段契约
# ---------------------------------------------------------------------------


class TestSearchEndpoint:
    def test_search_happy_path_hits(self, app_and_client):
        """POST /search '庆大霉素 儿童' 应命中 rx-aminoglycoside-pediatric。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/search", json={"query": "庆大霉素 儿童 8 岁"}
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True
        assert "data" in payload
        assert payload["data"]["query"] == "庆大霉素 儿童 8 岁"
        assert payload["data"]["count"] >= 1
        rule_ids = [r["rule_id"] for r in payload["data"]["results"]]
        assert "rx-aminoglycoside-pediatric" in rule_ids
        # Validate the response envelope shape
        Draft202012Validator(SCHEMA_SEARCH_RESPONSE).validate(payload)

    def test_search_with_drug_class_filter(self, app_and_client):
        """POST /search + drug_class=儿科 仅返回儿科类目规则。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/search",
            json={"query": "儿童 用药", "drug_class": "儿科", "limit": 5},
        )
        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert results
        for r in results:
            assert r["drug_class"] == "儿科"

    def test_search_with_limit(self, app_and_client):
        """POST /search + limit=2 应最多返回 2 条。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/search", json={"query": "用药", "limit": 2}
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["results"]) <= 2

    def test_search_request_schema_validation(self, app_and_client):
        """POST /search 缺 query 字段应返回 422(Pydantic 校验失败)。"""
        _, client, _ = app_and_client
        resp = client.post("/search", json={})
        assert resp.status_code == 422
        # The error envelope is automatic on 422 from Pydantic; verify it has ok=False
        body = resp.json()
        # FastAPI's default 422 shape differs from our envelope — but it
        # still returns a non-ok response. We just assert it is rejected.
        assert body.get("ok") is False or "detail" in body

    def test_search_request_schema_jsonschema(self):
        """SCHEMA_SEARCH_REQUEST 字段必填集合对齐。"""
        validator = Draft202012Validator(SCHEMA_SEARCH_REQUEST)
        # Happy path
        validator.validate({"query": "庆大霉素 儿童"})
        # Missing required 'query' should fail
        with pytest.raises(Exception):
            validator.validate({})

    def test_search_empty_query_rejected_by_jsonschema(self):
        """SCHEMA_SEARCH_REQUEST.minLength=1 拦截空 query。"""
        validator = Draft202012Validator(SCHEMA_SEARCH_REQUEST)
        with pytest.raises(Exception):
            validator.validate({"query": ""})


# ---------------------------------------------------------------------------
# 2) POST /inspect — 规则全文 + 404 + include_body 开关
# ---------------------------------------------------------------------------


class TestInspectEndpoint:
    def test_inspect_returns_full_rule_with_body(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/inspect",
            json={"rule_id": "rx-aminoglycoside-pediatric"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["rule_id"] == "rx-aminoglycoside-pediatric"
        assert data["severity"] == "high"
        assert data["drug_class"] == "儿科"
        assert "body" in data and data["body"]
        Draft202012Validator(SCHEMA_INSPECT_REQUEST).validate(
            {"rule_id": "rx-aminoglycoside-pediatric"}
        )

    def test_inspect_no_body_flag(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/inspect",
            json={"rule_id": "rx-aminoglycoside-pediatric", "include_body": False},
        )
        assert resp.status_code == 200
        # include_body=False strips body from inspect_rule payload (handler
        # passes through the rule summary only — body field is absent).
        assert resp.json()["data"].get("body", "") == ""

    def test_inspect_rule_not_found_returns_404(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post("/inspect", json={"rule_id": "rx-no-such-rule"})
        assert resp.status_code == 404
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "RULE_NOT_FOUND"

    def test_inspect_invalid_rule_id_pattern(self, app_and_client):
        """rule_id 含大写字母应被 Pydantic pattern 拒绝。"""
        _, client, _ = app_and_client
        resp = client.post("/inspect", json={"rule_id": "RX-Invalid"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3) POST /load — 审方草稿 + order_context 回显 + evidence_excerpt
# ---------------------------------------------------------------------------


class TestLoadEndpoint:
    def test_load_composes_draft(self, app_and_client):
        _, client, _ = app_and_client
        order_ctx = {
            "patient_age": 8,
            "drug_name": "庆大霉素",
            "dose": "80mg iv qd",
            "department": "儿科",
        }
        resp = client.post(
            "/load",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": order_ctx,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["rule"]["rule_id"] == "rx-aminoglycoside-pediatric"
        assert data["order_context"] == order_ctx
        assert isinstance(data["evidence_excerpt"], str)
        assert 0 < len(data["evidence_excerpt"]) <= 200
        Draft202012Validator(SCHEMA_LOAD_RESPONSE).validate(body)

    def test_load_missing_order_context_rejected_by_pydantic(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/load",
            json={"rule_id": "rx-aminoglycoside-pediatric"},
        )
        assert resp.status_code == 422

    def test_load_unknown_rule_id(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/load",
            json={
                "rule_id": "rx-no-such-rule",
                "order_context": {"drug_name": "x"},
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4) POST /apply — 审方意见 + confirmed=False + audit 留痕
# ---------------------------------------------------------------------------


class TestApplyEndpoint:
    def test_apply_returns_his_review_field(self, app_and_client):
        _, client, audit_path = app_and_client
        order_ctx = {
            "patient_age": 8,
            "drug_name": "庆大霉素",
            "dose": "80mg iv qd",
            "department": "儿科",
        }
        resp = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": order_ctx,
                "operator": "pharmacist-001",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        applied = body["data"]["applied"]
        # Schema-level checks first
        Draft202012Validator(SCHEMA_HIS_REVIEW_FIELD).validate(applied)
        assert applied["rule_id"] == "rx-aminoglycoside-pediatric"
        assert applied["confirmed"] is False  # 本工具永不代签
        assert len(applied["order_hash"]) == 64  # sha256 hex
        assert len(applied["session_id"]) == 16  # sha256[:16] hex
        assert applied["operator"] == "pharmacist-001"
        # Response envelope shape
        Draft202012Validator(SCHEMA_APPLY_RESPONSE).validate(body)

    def test_apply_writes_audit_jsonl(self, app_and_client):
        _, client, audit_path = app_and_client
        order_ctx = {"drug_name": "庆大霉素", "patient_age": 8}
        resp = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": order_ctx,
                "operator": "pharmacist-002",
            },
        )
        assert resp.status_code == 200
        # audit.jsonl exists with one row
        assert audit_path.exists()
        lines = [
            line
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["rule_id"] == "rx-aminoglycoside-pediatric"
        assert row["operator"] == "pharmacist-002"
        assert row["confirmed"] is False
        assert row["action"] == "apply"

    def test_apply_idempotent_order_hash_and_session(self, app_and_client):
        """同 order_context 同 operator → 同 order_hash + 同 session_id。"""
        _, client, _ = app_and_client
        order_ctx = {"drug_name": "庆大霉素", "patient_age": 8, "dose": "80mg"}
        body_a = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": order_ctx,
                "operator": "pharmacist-003",
            },
        ).json()
        body_b = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                # Field order swapped; same logical payload
                "order_context": {
                    "dose": "80mg",
                    "patient_age": 8,
                    "drug_name": "庆大霉素",
                },
                "operator": "pharmacist-003",
            },
        ).json()
        a = body_a["data"]["applied"]
        b = body_b["data"]["applied"]
        assert a["order_hash"] == b["order_hash"]
        assert a["session_id"] == b["session_id"]

    def test_apply_missing_operator_rejected(self, app_and_client):
        """缺 operator → Pydantic minLength=1 拒绝。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": {"drug_name": "庆大霉素"},
                "operator": "",
            },
        )
        assert resp.status_code == 422

    def test_apply_unknown_rule_id(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/apply",
            json={
                "rule_id": "rx-no-such-rule",
                "order_context": {"drug_name": "x"},
                "operator": "pharmacist-001",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "RULE_NOT_FOUND"

    def test_apply_request_schema_jsonschema(self):
        validator = Draft202012Validator(SCHEMA_APPLY_REQUEST)
        with pytest.raises(Exception):
            validator.validate(
                {"rule_id": "rx-aminoglycoside-pediatric"}  # 缺 order_context + operator
            )

    # ------------------------------------------------------------------
    # Task 32: apply 返回 conflicts + recommended_rule_id 字段
    # ------------------------------------------------------------------
    def test_apply_response_includes_conflicts_field(self, app_and_client):
        """POST /apply 必须返回 conflicts 与 recommended_rule_id 字段(可能空)。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/apply",
            json={
                "rule_id": "rx-aminoglycoside-pediatric",
                "order_context": {
                    "patient_age": 8,
                    "drug_name": "庆大霉素",
                    "dose": "80mg iv qd",
                    "department": "儿科",
                },
                "operator": "pharmacist-conflict-001",
            },
        )
        assert resp.status_code == 200, resp.text
        applied = resp.json()["data"]["applied"]
        # 字段必须存在(对齐 contract.SCHEMA_HIS_REVIEW_FIELD)
        assert "conflicts" in applied
        assert "recommended_rule_id" in applied
        assert isinstance(applied["conflicts"], list)
        # 既有契约不变
        assert applied["confirmed"] is False
        assert applied["rule_id"] == "rx-aminoglycoside-pediatric"
        # 字段经 jsonschema 双向校验仍合法
        Draft202012Validator(SCHEMA_HIS_REVIEW_FIELD).validate(applied)


# ---------------------------------------------------------------------------
# 5) POST /audit/export — 月度 CSV 导出
# ---------------------------------------------------------------------------


class TestAuditExportEndpoint:
    def _seed_audit(self, audit_path: Path, month: str = "2026-09"):
        """Pre-seed two audit rows for the month."""
        import datetime as _dt

        path = audit_path
        rows = [
            {
                "timestamp": f"{month}-01T08:00:00+00:00",
                "action": "apply",
                "rule_id": "rx-aminoglycoside-pediatric",
                "operator": "pharmacist-A",
                "session_id": "deadbeef00000001",
                "order_hash": "a" * 64,
                "confirmed": False,
            },
            {
                "timestamp": f"{month}-15T14:30:00+00:00",
                "action": "apply",
                "rule_id": "rx-nsaid-pregnancy",
                "operator": "pharmacist-B",
                "session_id": "deadbeef00000002",
                "order_hash": "b" * 64,
                "confirmed": False,
            },
        ]
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False))
                fh.write("\n")
        return rows

    def test_audit_export_happy_path(self, app_and_client):
        _, client, audit_path = app_and_client
        self._seed_audit(audit_path)
        resp = client.post(
            "/audit/export", json={"month": "2026-09", "out": str(audit_path.parent / "audit.csv")}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["data"]["month"] == "2026-09"
        assert body["data"]["events"] == 2
        out_path = Path(body["data"]["out_path"])
        assert out_path.exists()
        # CSV BOM + 列头 + 2 行
        raw = out_path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM
        text = raw.decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines[0].startswith("timestamp")
        assert len(lines) == 3  # 1 header + 2 data
        Draft202012Validator(SCHEMA_AUDIT_EXPORT_RESPONSE).validate(body)

    def test_audit_export_no_audit_file_returns_400(self, app_and_client):
        _, client, audit_path = app_and_client
        # Don't seed; the fixture already gave us an empty audit dir but
        # audit.jsonl doesn't exist
        assert not audit_path.exists()
        resp = client.post(
            "/audit/export", json={"month": "2026-09"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_INPUT"

    def test_audit_export_request_schema_validation(self):
        validator = Draft202012Validator(SCHEMA_AUDIT_EXPORT_REQUEST)
        with pytest.raises(Exception):
            validator.validate({"month": "2026/09"})  # 错误分隔符
        validator.validate({"month": "2026-09"})


# ---------------------------------------------------------------------------
# 6) OpenAPI 文档:5 路由 + 信息字段 + mock disclaimer
# ---------------------------------------------------------------------------


class TestOpenAPI:
    def test_openapi_json_includes_five_routes(self, app_and_client):
        application, _, _ = app_and_client
        client = TestClient(application)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["info"]["title"] == "用药字段对照工作台 REST"
        assert spec["info"]["version"] == "0.1.0"
        paths = spec["paths"]
        # 5 POST routes
        assert "/search" in paths
        assert "/inspect" in paths
        assert "/load" in paths
        assert "/apply" in paths
        assert "/audit/export" in paths
        # All are POST
        for path in (
            "/search",
            "/inspect",
            "/load",
            "/apply",
            "/audit/export",
        ):
            assert "post" in paths[path]

    def test_openapi_includes_mock_disclaimer(self, app_and_client):
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        description = spec["info"]["description"]
        assert "mock" in description or "对接层" in description

    def test_openapi_includes_request_response_schemas(self, app_and_client):
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        # Pydantic-generated component schemas should at least include our
        # Request/Response models (by class name).
        schemas = spec.get("components", {}).get("schemas", {})
        assert any("SearchRequest" in name for name in schemas)
        assert any("ApplyRequest" in name for name in schemas)
        assert any("HisReviewField" in name for name in schemas)

    def test_docs_route_returns_swagger_html(self, app_and_client):
        application, _, _ = app_and_client
        client = TestClient(application)
        resp = client.get("/docs")
        assert resp.status_code == 200
        text = resp.text
        assert "swagger" in text.lower() or "openapi" in text.lower()

    def test_redoc_route_returns_redoc_html(self, app_and_client):
        application, _, _ = app_and_client
        client = TestClient(application)
        resp = client.get("/redoc")
        assert resp.status_code == 200

    # ------------------------------------------------------------------
    # Task 23 additions: custom_openapi 显式 tags / contract 对齐校验
    # ------------------------------------------------------------------

    def test_openapi_includes_explicit_tag_groups(self, app_and_client):
        """custom_openapi 应暴露 '审方' / '审计' 两个 tag 分组。

        任务 23 步骤要求:tags=[{name: '审方', description: '...'},
        {name: '审计', description: '...'}]。验证 OpenAPI tags 数组
        含这两个 name,且 description 非空。
        """
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        tags = spec.get("tags", [])
        tag_names = [t["name"] for t in tags]
        assert "审方" in tag_names, f"missing '审方' tag in {tag_names}"
        assert "审计" in tag_names, f"missing '审计' tag in {tag_names}"
        # Each tag must carry a non-empty description
        for t in tags:
            if t["name"] in ("审方", "审计"):
                assert t.get("description"), f"tag {t['name']} missing description"

    def test_openapi_tag_grouping_on_routes(self, app_and_client):
        """5 个路由应挂载到对应的 tag 分组(便于 Swagger UI 折叠)。

        POST /search /inspect /load /apply → '审方'
        POST /audit/export                → '审计'
        """
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        rx_paths = ("/search", "/inspect", "/load", "/apply")
        for path in rx_paths:
            post = paths[path]["post"]
            assert "审方" in post.get("tags", []), (
                f"{path} 应挂 '审方' tag,got {post.get('tags')}"
            )
        assert "审计" in paths["/audit/export"]["post"].get("tags", [])

    def test_openapi_contact_and_license(self, app_and_client):
        """custom_openapi 应补充 contact(内网标识 x-internal-only=True)
        与 license(MIT)。
        """
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        info = spec["info"]
        assert info.get("contact", {}).get("x-internal-only") is True
        assert info.get("license", {}).get("name") == "MIT"

    def test_openapi_servers_declares_default_endpoint(self, app_and_client):
        """servers 列表应声明默认 127.0.0.1:<port>,便于厂家对接定位。"""
        application, _, _ = app_and_client
        client = TestClient(application)
        spec = client.get("/openapi.json").json()
        servers = spec.get("servers", [])
        assert servers, "openapi.servers missing"
        default_url = servers[0].get("url", "")
        assert default_url.startswith("http://"), default_url
        assert "127.0.0.1" in default_url


# ---------------------------------------------------------------------------
# 7) Search → inspect 链路烟测(对齐 check-hermes-plugin.py 烟测契约)
# ---------------------------------------------------------------------------


class TestSearchToInspectChain:
    def test_search_then_inspect_same_rule(self, app_and_client):
        _, client, _ = app_and_client
        search_resp = client.post(
            "/search", json={"query": "庆大霉素 儿童 8 岁"}
        )
        assert search_resp.status_code == 200
        top1 = search_resp.json()["data"]["results"][0]
        rule_id = top1["rule_id"]
        # Inspect the top-1 hit
        inspect_resp = client.post(
            "/inspect", json={"rule_id": rule_id}
        )
        assert inspect_resp.status_code == 200
        inspect_data = inspect_resp.json()["data"]
        assert inspect_data["rule_id"] == rule_id
        assert inspect_data["severity"] == top1["severity"]
        assert inspect_data["drug_class"] == top1["drug_class"]


# ---------------------------------------------------------------------------
# 8) Error envelope:统一 {ok: False, error: {code, message}}
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_404_uses_envelope_shape(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/inspect", json={"rule_id": "rx-no-such-rule"}
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["ok"] is False
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_pydantic_422_returns_validation_detail(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/search", json={"query": ""}  # minLength=1 violation
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 9) Task 24:统一 4xx/5xx + Pydantic 422 + ErrorCode 枚举
# ---------------------------------------------------------------------------


class TestTask24UnifiedErrorHandling:
    """Task 24:统一错误处理契约。

    - test_api_invalid_query:handler 层空白 query → 400 + INVALID_INPUT
      (Pydantic min_length=1 只拦空字符串;handler 层再拦全空白字符串)
    - test_api_rule_not_found:404 + RULE_NOT_FOUND + envelope
    - test_api_validation_error:缺必填字段 → 422 + INVALID_INPUT 包装
    - test_api_internal_error:monkeypatch 触发 RuntimeError → 500 + INTERNAL_ERROR
    """

    def test_api_invalid_query_whitespace_returns_400(
        self, app_and_client
    ):
        """全空白 query(Pydantic 通过但无意义) → 400 INVALID_INPUT。"""
        _, client, _ = app_and_client
        resp = client.post("/search", json={"query": "   "})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
        assert "query" in body["error"]["message"]

    def test_api_invalid_query_empty_string_returns_422(
        self, app_and_client
    ):
        """空字符串 query → 422(Pydantic minLength=1 先拦下)。"""
        _, client, _ = app_and_client
        resp = client.post("/search", json={"query": ""})
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
        assert "Pydantic" in body["error"]["message"]
        assert isinstance(body["error"]["detail"], list)

    def test_api_rule_not_found_returns_404_envelope(
        self, app_and_client
    ):
        """不存在的 rule_id → 404 + RULE_NOT_FOUND。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/inspect", json={"rule_id": "rx-this-does-not-exist"}
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "RULE_NOT_FOUND"
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]

    def test_api_validation_error_missing_required_field(
        self, app_and_client
    ):
        """缺必填字段(query) → 422 + INVALID_INPUT 包装。"""
        _, client, _ = app_and_client
        resp = client.post("/search", json={})  # 缺 query
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
        # detail 应列出 Pydantic 校验错误
        assert isinstance(body["error"]["detail"], list)
        assert any("query" in str(d) for d in body["error"]["detail"])

    def test_api_validation_error_bad_pattern(
        self, app_and_client
    ):
        """rule_id 不符 kebab-case pattern → 422 + INVALID_INPUT。"""
        _, client, _ = app_and_client
        resp = client.post(
            "/inspect", json={"rule_id": "INVALID_UPPERCASE"}
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_api_internal_error_returns_500_envelope(
        self, app_and_client, monkeypatch
    ):
        """monkeypatch 触发 RuntimeError → 500 + INTERNAL_ERROR。

        使用 raise_server_exceptions=False 让 FastAPI 异常处理器兜底,
        而非让 TestClient 把异常上抛到 pytest。
        """
        application, _, _ = app_and_client
        client = TestClient(application, raise_server_exceptions=False)

        def _boom(*_a, **_kw):
            raise RuntimeError("synthetic internal failure")

        # Patch search_rules to raise an unexpected exception
        from pass_field_check import api as api_module

        monkeypatch.setattr(api_module, "search_rules", _boom)

        resp = client.post(
            "/search", json={"query": "庆大霉素 儿童"}
        )
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "synthetic internal failure" in body["error"]["message"]

    def test_api_error_envelope_matches_schema(self, app_and_client):
        """所有 4xx/5xx 响应都应满足 SCHEMA_ERROR_RESPONSE。"""
        from pass_field_check.contract import SCHEMA_ERROR_RESPONSE

        _, client, _ = app_and_client
        # 触发 404
        resp_404 = client.post(
            "/inspect", json={"rule_id": "rx-nope"}
        )
        Draft202012Validator(SCHEMA_ERROR_RESPONSE).validate(resp_404.json())
        # 触发 422
        resp_422 = client.post("/search", json={})
        Draft202012Validator(SCHEMA_ERROR_RESPONSE).validate(resp_422.json())


# ---------------------------------------------------------------------------
# GET /audit/events — 检索 / 过滤 / 分页契约
# ---------------------------------------------------------------------------


class TestAuditEventsEndpoint:
    """GET /audit/events 路由契约:多条件过滤 + 分页 + 错误处理。"""

    @staticmethod
    def _append_event(audit_path: Path, **fields) -> None:
        """通过 /apply 路由 + 模拟直接 append 两种方式均可,这里用直接 append。"""
        record = {
            "timestamp": fields.get(
                "timestamp", "2026-09-08T01:00:00+00:00"
            ),
            "rule_id": fields.get(
                "rule_id", "rx-aminoglycoside-pediatric"
            ),
            "order_hash": fields.get("order_hash", "a" * 64),
            "session_id": fields.get("session_id", "b" * 16),
            "operator": fields.get("operator", "pharmacist-001"),
            "action": fields.get("action", "apply"),
            "confirmed": fields.get("confirmed", False),
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")

    def test_audit_events_get_happy_path(self, app_and_client):
        """GET /audit/events 无过滤 → 返回全部 + 标准 envelope。"""
        _, client, audit_path = app_and_client
        # 写 3 条
        for op in ("alice", "bob", "alice"):
            self._append_event(audit_path, operator=op)
        resp = client.get("/audit/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["page_size"] == 50
        assert len(data["events"]) == 3

    def test_audit_events_filter_by_operator(self, app_and_client):
        """?operator=alice 仅返回 alice 的事件。"""
        _, client, audit_path = app_and_client
        for op in ("alice", "bob", "alice", "carol"):
            self._append_event(audit_path, operator=op)
        resp = client.get("/audit/events", params={"operator": "alice"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert all(ev["operator"] == "alice" for ev in data["events"])

    def test_audit_events_filter_by_rule_id(self, app_and_client):
        _, client, audit_path = app_and_client
        self._append_event(
            audit_path, rule_id="rx-aminoglycoside-pediatric"
        )
        self._append_event(
            audit_path, rule_id="rx-nsaid-pregnancy"
        )
        self._append_event(
            audit_path, rule_id="rx-aminoglycoside-pediatric"
        )
        resp = client.get(
            "/audit/events",
            params={"rule_id": "rx-aminoglycoside-pediatric"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert all(
            ev["rule_id"] == "rx-aminoglycoside-pediatric"
            for ev in data["events"]
        )

    def test_audit_events_filter_by_date_range(self, app_and_client):
        """?start_date=...&end_date=... 半开区间过滤。"""
        _, client, audit_path = app_and_client
        for ts in [
            "2026-08-15T08:00:00+00:00",
            "2026-09-01T08:00:00+00:00",
            "2026-09-15T08:00:00+00:00",
            "2026-10-01T08:00:00+00:00",
        ]:
            self._append_event(audit_path, timestamp=ts)
        resp = client.get(
            "/audit/events",
            params={
                "start_date": "2026-09-01T00:00:00+00:00",
                "end_date": "2026-10-01T00:00:00+00:00",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2  # 9-01 + 9-15
        for ev in data["events"]:
            assert ev["timestamp"].startswith("2026-09")

    def test_audit_events_filter_by_action(self, app_and_client):
        _, client, audit_path = app_and_client
        for action in ("apply", "search", "apply", "inspect"):
            self._append_event(audit_path, action=action)
        resp = client.get(
            "/audit/events", params={"action": "apply"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert all(ev["action"] == "apply" for ev in data["events"])

    def test_audit_events_pagination(self, app_and_client):
        """page=1 / page=2 / page=3 拼接 = 全量。"""
        _, client, audit_path = app_and_client
        # 5 条不同时间戳
        for i in range(5):
            self._append_event(
                audit_path,
                timestamp=f"2026-09-{(i + 1):02d}T0{i}:00:00+00:00",
            )
        # page=1 size=2
        resp1 = client.get(
            "/audit/events", params={"page": 1, "page_size": 2}
        )
        assert resp1.status_code == 200
        data1 = resp1.json()["data"]
        assert data1["total"] == 5
        assert data1["page"] == 1
        assert data1["page_size"] == 2
        assert len(data1["events"]) == 2
        # page=2 size=2
        resp2 = client.get(
            "/audit/events", params={"page": 2, "page_size": 2}
        )
        data2 = resp2.json()["data"]
        assert data2["total"] == 5
        assert len(data2["events"]) == 2
        # page=3 size=2 (只有 1 条)
        resp3 = client.get(
            "/audit/events", params={"page": 3, "page_size": 2}
        )
        data3 = resp3.json()["data"]
        assert len(data3["events"]) == 1
        # 三页拼接 = 5 条且无重复
        all_ts = (
            [ev["timestamp"] for ev in data1["events"]]
            + [ev["timestamp"] for ev in data2["events"]]
            + [ev["timestamp"] for ev in data3["events"]]
        )
        assert len(set(all_ts)) == 5

    def test_audit_events_response_envelope_schema(self, app_and_client):
        """响应满足 SCHEMA_AUDIT_QUERY_RESPONSE 契约。"""
        from pass_field_check.contract import SCHEMA_AUDIT_QUERY_RESPONSE

        _, client, audit_path = app_and_client
        self._append_event(audit_path)
        resp = client.get("/audit/events")
        body = resp.json()
        Draft202012Validator(SCHEMA_AUDIT_QUERY_RESPONSE).validate(body)

    def test_audit_events_missing_audit_file_returns_empty(
        self, app_and_client
    ):
        """audit.jsonl 不存在 → 200 + 空列表(便于 frontend 容忍冷启动)。"""
        _, client, _ = app_and_client
        resp = client.get("/audit/events")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 0
        assert data["events"] == []
        assert data["page"] == 1
        assert data["page_size"] == 50

    def test_audit_events_invalid_date_format_400(self, app_and_client):
        """start_date 非 ISO 8601 → 400 INVALID_INPUT。"""
        _, client, _ = app_and_client
        resp = client.get(
            "/audit/events", params={"start_date": "2026/09/01"}
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_audit_events_start_after_end_400(self, app_and_client):
        """start_date > end_date → 400 INVALID_INPUT。"""
        _, client, _ = app_and_client
        resp = client.get(
            "/audit/events",
            params={
                "start_date": "2026-09-30T00:00:00+00:00",
                "end_date": "2026-09-01T00:00:00+00:00",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
        assert "start_date" in body["error"]["message"]

    def test_audit_events_invalid_action_pattern_422(self, app_and_client):
        """action ∉ {search, inspect, load, apply} → 422 (Pydantic pattern 校验)。"""
        _, client, _ = app_and_client
        resp = client.get(
            "/audit/events", params={"action": "delete"}
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_audit_events_invalid_rule_id_pattern_422(self, app_and_client):
        """rule_id 不匹配 ^[a-z0-9][a-z0-9-]*$ → 422。"""
        _, client, _ = app_and_client
        resp = client.get(
            "/audit/events", params={"rule_id": "Bad_Rule!"}
        )
        assert resp.status_code == 422

    def test_audit_events_page_size_over_max_422(self, app_and_client):
        """page_size > 500 → 422 (FastAPI Query le=500 拦截)。"""
        _, client, _ = app_and_client
        resp = client.get(
            "/audit/events", params={"page_size": 1000}
        )
        assert resp.status_code == 422

    def test_audit_events_openapi_lists_route(self, app_and_client):
        """OpenAPI schema 含 GET /audit/events 路径。"""
        application, _, _ = app_and_client
        schema = application.openapi()
        assert "/audit/events" in schema["paths"]
        assert "get" in schema["paths"]["/audit/events"]
        params = schema["paths"]["/audit/events"]["get"].get("parameters", [])
        param_names = {p["name"] for p in params}
        # 7 个 query params:operator / rule_id / start_date / end_date / action / page / page_size
        assert {
            "operator",
            "rule_id",
            "start_date",
            "end_date",
            "action",
            "page",
            "page_size",
        }.issubset(param_names)