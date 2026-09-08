"""端到端 smoke 测试:启动 uvicorn + 跑 CLI 全链路 + REST + audit 导出。

测试目标:
  1. 启动 uvicorn(后台进程,端口 18765);
  2. CLI pass-fc index-build 重建 12 条规则索引;
  3. CLI pass-fc search 命中目标规则;
  4. CLI pass-fc apply 触发 audit.jsonl 追加;
  5. REST POST /search / /apply 端到端可访问且与 CLI 命中一致;
  6. REST GET /audit/events?operator=... 命中 CLI apply 的事件;
  7. CLI pass-fc audit-export --month 导出 CSV 含 BOM + 列头 + 行数 ≥ 2;
  8. 全链路无 5xx;CSV 头与 audit 字段严格对齐 contract.py 公共子集。

本测试使用临时工作目录(tmp_path)隔离,所有运行时数据 / log 落在 tmp 下,
不污染仓库 round_XXX 的 audit/ / data/ / logs/ 目录。

**注意**:本测试只验证产品主路径(端到端可跑通 + 字段契约一致),不重复单模块
单元测试场景;详细断言见 tests/test_api.py / test_cli.py / test_audit.py。
"""

from __future__ import annotations

import csv
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
RULES_DIR = REPO_ROOT / "rules"
RULES_INDEX = REPO_ROOT / "rules_index.json"

# E2E 专用端口(避开默认 8765 便于 CI / 本地并行)
E2E_PORT = 18765
E2E_HOST = "127.0.0.1"

# 测试 operator(占位工号)
E2E_OPERATOR = "pharmacist-e2e-001"

# 启动 uvicorn 等待超时
UVICORN_READY_TIMEOUT_SEC = 15
UVICORN_POLL_INTERVAL_SEC = 0.2


# ---------------------------------------------------------------------------
# 辅助:端口 / uvicorn 生命周期
# ---------------------------------------------------------------------------


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """检查端口是否已被占用(便于跳过 18765 已被本机其他进程占用的场景)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect((host, port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False


def _wait_for_uvicorn_ready(host: str, port: int, timeout: float) -> bool:
    """轮询 /openapi.json 直到 200 或超时。"""
    deadline = time.monotonic() + timeout
    url = f"http://{host}:{port}/openapi.json"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    return True
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        time.sleep(UVICORN_POLL_INTERVAL_SEC)
    return False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_workspace(tmp_path: Path) -> dict[str, Path]:
    """构造 E2E 隔离工作区:audit.jsonl / rules.json / 临时 CLI home。

    复用仓库自带的 12 条规则文件 + rules_index.json(只读源),但 CLI 输出的
    audit.jsonl / data/rules.json 通过全局参数 --audit-path / --out
    重定向到 tmp_path,避免污染 round 根目录的运行时数据。
    """
    audit_path = tmp_path / "audit" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    csv_out = tmp_path / "export.csv"
    server_log = tmp_path / "uvicorn.log"

    return {
        "audit_path": audit_path,
        "data_dir": data_dir,
        "csv_out": csv_out,
        "server_log": server_log,
        "tmp_root": tmp_path,
    }


@pytest.fixture
def uvicorn_server(e2e_workspace: dict[str, Path]) -> dict[str, Any]:
    """后台启动 uvicorn 实例;teardown 时关停。"""
    audit_path: Path = e2e_workspace["audit_path"]
    server_log: Path = e2e_workspace["server_log"]

    if _port_in_use(E2E_PORT):
        pytest.skip(f"E2E 端口 {E2E_PORT} 已被占用,跳过本测试")

    # 用临时 PYTHONPATH 注入包路径,避免 editable install 未生效场景
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 关键:让 REST API 与 CLI 共用同一 audit.jsonl(api._resolve_default_audit_path
    # 读 RX_AUDIT_PATH),否则 REST apply 写到 REPO_ROOT/audit/audit.jsonl 而非 tmp
    env["RX_AUDIT_PATH"] = str(audit_path)

    log_handle = open(server_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "pass_field_check.api:app",
            "--host",
            E2E_HOST,
            "--port",
            str(E2E_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        # 新进程组便于整组 kill
        preexec_fn=os.setsid,
    )

    # 等待 /openapi.json 200
    ready = _wait_for_uvicorn_ready(E2E_HOST, E2E_PORT, UVICORN_READY_TIMEOUT_SEC)
    if not ready:
        proc.terminate()
        log_handle.close()
        pytest.fail(
            f"uvicorn 在 {UVICORN_READY_TIMEOUT_SEC}s 内未就绪;日志:\n"
            f"{server_log.read_text(encoding='utf-8', errors='replace')}"
        )

    yield {
        "proc": proc,
        "log_handle": log_handle,
        "base_url": f"http://{E2E_HOST}:{E2E_PORT}",
    }

    # teardown:优雅关停
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    finally:
        log_handle.close()


# ---------------------------------------------------------------------------
# CLI 辅助
# ---------------------------------------------------------------------------


def _run_cli(
    args: list[str],
    *,
    audit_path: Path | None = None,
    rules_dir: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """运行 pass-fc CLI 子进程;返回 CompletedProcess(便于断言 stdout/stderr/exit)。

    全局参数(--rules-dir / --audit-path)在子命令**之后**追加,匹配
    argparse parents=[_COMMON] 的解析顺序(子命令后才能跟全局参数)。
    """
    cmd = [sys.executable, "-m", "pass_field_check.cli", *args]
    if rules_dir is not None:
        cmd += ["--rules-dir", str(rules_dir)]
    if audit_path is not None:
        cmd += ["--audit-path", str(audit_path)]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 关闭颜色便于解析
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"

    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# 端到端测试
# ---------------------------------------------------------------------------


class TestEndToEndSmoke:
    """全链路 smoke:CLI → REST → audit → CSV export。"""

    def test_full_pipeline_cli_to_rest_to_csv(
        self,
        e2e_workspace: dict[str, Path],
        uvicorn_server: dict[str, Any],
    ) -> None:
        """端到端 7 步全链路。"""
        audit_path: Path = e2e_workspace["audit_path"]
        data_dir: Path = e2e_workspace["data_dir"]
        csv_out: Path = e2e_workspace["csv_out"]
        base_url: str = uvicorn_server["base_url"]

        # ------------------------------------------------------------------
        # Step 1: CLI index-build → 重建 12 条规则索引到 tmp
        # ------------------------------------------------------------------
        out_path = data_dir / "rules.json"
        build = _run_cli(
            [
                "index-build",
                "--rules-dir",
                str(RULES_DIR),
                "--out",
                str(out_path),
                "--index",
                str(RULES_INDEX),
            ],
            audit_path=audit_path,
            rules_dir=RULES_DIR,
        )
        assert build.returncode == 0, (
            f"index-build 失败;exit={build.returncode}, "
            f"stderr={build.stderr!r}, stdout={build.stdout!r}"
        )
        assert out_path.exists(), "data/rules.json 未生成"
        rules_doc = json.loads(out_path.read_text(encoding="utf-8"))
        assert isinstance(rules_doc.get("rules"), list), "rules 数组缺失"
        assert len(rules_doc["rules"]) >= 12, (
            f"应至少 12 条规则;实际 {len(rules_doc['rules'])}"
        )

        # ------------------------------------------------------------------
        # Step 2: CLI search → 命中 rx-aminoglycoside-pediatric
        # ------------------------------------------------------------------
        search = _run_cli(
            [
                "search",
                "庆大霉素 儿童 8 岁",
                "--limit",
                "3",
                "--no-color",
            ],
            audit_path=audit_path,
            rules_dir=RULES_DIR,
        )
        assert search.returncode == 0, (
            f"CLI search 失败;exit={search.returncode}, "
            f"stderr={search.stderr!r}, stdout={search.stdout!r}"
        )
        assert "rx-aminoglycoside-pediatric" in search.stdout, (
            f"search 输出应含目标 rule_id;实际 stdout={search.stdout!r}"
        )

        # ------------------------------------------------------------------
        # Step 3: CLI apply → 留痕 audit.jsonl
        # ------------------------------------------------------------------
        order_context_json = json.dumps(
            {
                "patient_age": 8,
                "drug_name": "庆大霉素",
                "dose": "80mg",
                "route": "iv",
                "department": "儿科",
            },
            ensure_ascii=False,
        )
        apply = _run_cli(
            [
                "apply",
                "--rule-id",
                "rx-aminoglycoside-pediatric",
                "--order-context",
                order_context_json,
                "--operator",
                E2E_OPERATOR,
            ],
            audit_path=audit_path,
            rules_dir=RULES_DIR,
        )
        assert apply.returncode == 0, (
            f"CLI apply 失败;exit={apply.returncode}, "
            f"stderr={apply.stderr!r}, stdout={apply.stdout!r}"
        )
        apply_out = json.loads(apply.stdout)
        assert apply_out.get("success") is True
        applied = apply_out.get("applied", {})
        assert applied.get("rule_id") == "rx-aminoglycoside-pediatric"
        assert applied.get("confirmed") is False, "本工具永不代签"
        assert applied.get("operator") == E2E_OPERATOR
        assert len(applied.get("order_hash", "")) == 64
        assert len(applied.get("session_id", "")) == 16

        # audit.jsonl 应至少 1 行
        assert audit_path.exists(), "audit.jsonl 未生成"
        with audit_path.open("r", encoding="utf-8") as f:
            audit_lines = [ln for ln in f if ln.strip()]
        assert len(audit_lines) >= 1, f"audit.jsonl 应至少 1 行;实际 {len(audit_lines)}"
        last_audit = json.loads(audit_lines[-1])
        assert last_audit.get("action") == "apply"
        assert last_audit.get("rule_id") == "rx-aminoglycoside-pediatric"
        assert last_audit.get("operator") == E2E_OPERATOR
        assert last_audit.get("confirmed") is False

        # ------------------------------------------------------------------
        # Step 4: REST POST /search 与 CLI 命中一致
        # ------------------------------------------------------------------
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            rest_search = client.post(
                "/search",
                json={"query": "庆大霉素 儿童 8 岁", "limit": 3},
            )
        assert rest_search.status_code == 200, (
            f"REST /search 非 200;actual={rest_search.status_code}, "
            f"body={rest_search.text!r}"
        )
        rest_search_body = rest_search.json()
        assert rest_search_body.get("ok") is True
        rest_results = rest_search_body["data"]["results"]
        assert any(
            r.get("rule_id") == "rx-aminoglycoside-pediatric" for r in rest_results
        ), f"REST /search 应命中目标 rule;实际 {rest_results!r}"

        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            rest_apply = client.post(
                "/apply",
                json={
                    "rule_id": "rx-aminoglycoside-pediatric",
                    "order_context": {
                        "patient_age": 8,
                        "drug_name": "庆大霉素",
                        "dose": "80mg",
                        "route": "iv",
                        "department": "儿科",
                    },
                    "operator": E2E_OPERATOR,
                },
            )
        assert rest_apply.status_code == 200, (
            f"REST /apply 非 200;actual={rest_apply.status_code}, "
            f"body={rest_apply.text!r}"
        )
        rest_apply_body = rest_apply.json()
        assert rest_apply_body.get("ok") is True
        # 响应结构:{"ok": true, "data": {"applied": {rule_id, ...}}}
        rest_data = rest_apply_body["data"]
        rest_applied = rest_data["applied"]
        assert rest_applied.get("rule_id") == "rx-aminoglycoside-pediatric"
        assert rest_applied.get("confirmed") is False
        assert rest_applied.get("order_hash") == applied.get("order_hash"), (
            "REST apply 与 CLI apply 同 order_context 应得同 order_hash"
        )

        # ------------------------------------------------------------------
        # Step 6: REST GET /audit/events?operator=... → 命中两条 apply 事件
        # ------------------------------------------------------------------
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            rest_audit = client.get(
                "/audit/events",
                params={"operator": E2E_OPERATOR, "page": 1, "page_size": 50},
            )
        assert rest_audit.status_code == 200, (
            f"REST /audit/events 非 200;actual={rest_audit.status_code}, "
            f"body={rest_audit.text!r}"
        )
        audit_body = rest_audit.json()
        assert audit_body.get("ok") is True
        events = audit_body["data"]["events"]
        apply_events = [e for e in events if e.get("action") == "apply"]
        assert len(apply_events) >= 2, (
            f"应至少 2 条 apply 事件(CLI + REST);实际 {len(apply_events)}"
        )
        # 两条都来自 E2E_OPERATOR
        for ev in apply_events:
            assert ev.get("operator") == E2E_OPERATOR
            assert ev.get("confirmed") is False
            assert ev.get("rule_id") == "rx-aminoglycoside-pediatric"

        # ------------------------------------------------------------------
        # Step 7: CLI audit-export --month → CSV 含 BOM + 列头 + 行数 ≥ 2
        # ------------------------------------------------------------------
        current_month = time.strftime("%Y-%m")
        export = _run_cli(
            [
                "audit-export",
                "--month",
                current_month,
                "--out",
                str(csv_out),
            ],
            audit_path=audit_path,
            rules_dir=RULES_DIR,
        )
        assert export.returncode == 0, (
            f"audit-export 失败;exit={export.returncode}, "
            f"stderr={export.stderr!r}, stdout={export.stdout!r}"
        )
        assert csv_out.exists(), "CSV 未生成"

        # 校验 utf-8-sig BOM
        raw_bytes = csv_out.read_bytes()
        assert raw_bytes[:3] == b"\xef\xbb\xbf", (
            f"CSV 应含 utf-8-sig BOM;实际首 3 字节={raw_bytes[:3]!r}"
        )

        # 校验 CSV 列头 + 行数
        csv_text = raw_bytes.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) >= 3, (
            f"CSV 应至少 3 行(1 表头 + 2 数据);实际 {len(rows)}"
        )
        header = rows[0]
        required_cols = {"timestamp", "rule_id", "action", "operator", "confirmed"}
        assert required_cols.issubset(set(header)), (
            f"CSV 头缺关键列;header={header!r}, "
            f"missing={required_cols - set(header)}"
        )

        # 至少 2 行 apply 数据,operator 与 confirmed 字段对齐
        apply_rows = [r for r in rows[1:] if len(r) >= len(header) and r[header.index("action")] == "apply"]
        assert len(apply_rows) >= 2, (
            f"应至少 2 行 apply 数据;实际 {len(apply_rows)}"
        )
        for r in apply_rows:
            assert r[header.index("operator")] == E2E_OPERATOR
            assert r[header.index("confirmed")] == "False"
            assert r[header.index("rule_id")] == "rx-aminoglycoside-pediatric"

    def test_openapi_and_docs_routes_accessible(
        self,
        uvicorn_server: dict[str, Any],
    ) -> None:
        """REST 三件套(/openapi.json / /docs / /redoc)端到端可访问。"""
        base_url: str = uvicorn_server["base_url"]
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            openapi = client.get("/openapi.json")
            docs = client.get("/docs")
            redoc = client.get("/redoc")
        assert openapi.status_code == 200
        assert docs.status_code == 200
        assert redoc.status_code == 200
        spec = openapi.json()
        # 关键 5 路由全部存在
        for path in ("/search", "/inspect", "/load", "/apply", "/audit/export"):
            assert path in spec.get("paths", {}), f"OpenAPI 缺路由 {path}"
        # info.title 中文产品名
        assert "用药字段对照" in spec.get("info", {}).get("title", ""), (
            f"OpenAPI title 应含中文产品名;actual="
            f"{spec.get('info', {}).get('title', '')!r}"
        )
        # /docs 应含 Swagger UI 标识
        assert "swagger" in docs.text.lower(), "/docs 应为 Swagger UI"
