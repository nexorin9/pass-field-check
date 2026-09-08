"""CLI 集成测试:subprocess 跑 pass-fc 子命令验证。

源码产品能力参考(参考地基 / evidence chain):
  github_ref/agency-agents/scripts/check-hermes-plugin.py
    - subprocess.run (L40-60) → 复用其 启动子进程 + 捕获 stdout/stderr + 退出码
      断言(0 = ok,非零 = fail)思路,与 Hermes smoke 烟测契约一致。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `python -m pass_field_check.cli <args>` from the project root."""
    cmd = [sys.executable, "-m", "pass_field_check.cli", *args]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module", autouse=True)
def _ensure_index_built() -> None:
    """Build data/rules.json once for the whole test module if missing."""
    index_path = PROJECT_ROOT / "data" / "rules.json"
    if index_path.exists():
        return
    result = _run_cli("index-build")
    assert result.returncode == 0, f"index-build failed: {result.stderr}"


# ---------------------------------------------------------------------------
# search 子命令
# ---------------------------------------------------------------------------


def test_cli_search_aminoglycoside_pediatric_human_table() -> None:
    """search '庆大霉素 儿童' → top-N 含 rx-aminoglycoside-pediatric (人类表格)."""
    result = _run_cli("search", "庆大霉素 儿童")
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "rx-aminoglycoside-pediatric" in result.stdout
    # 表格头校验
    assert "rule_id" in result.stdout
    assert "severity" in result.stdout


def test_cli_search_aminoglycoside_pediatric_json() -> None:
    """search --json 输出合法 JSON 数组 + 命中目标 rule."""
    result = _run_cli("search", "庆大霉素 儿童", "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) > 0
    rule_ids = {item["rule_id"] for item in data}
    assert "rx-aminoglycoside-pediatric" in rule_ids


def test_cli_search_drug_class_filter() -> None:
    """--drug-class 过滤生效 (query='布洛芬' + drug_class='孕期' → 命中孕期规则)."""
    result = _run_cli(
        "search",
        "布洛芬",
        "--drug-class",
        "孕期",
        "--json",
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert len(data) > 0
    assert all(item["drug_class"] == "孕期" for item in data)
    rule_ids = {item["rule_id"] for item in data}
    assert "rx-nsaid-pregnancy" in rule_ids


def test_cli_search_no_match_returns_empty() -> None:
    """query 完全无命中时返回 '(no matches)'."""
    result = _run_cli(
        "search",
        "xyz不存在的中文和英文无关词abc12345",
    )
    assert result.returncode == 0
    assert "no matches" in result.stdout.lower() or "无命中" in result.stdout


# ---------------------------------------------------------------------------
# inspect 子命令
# ---------------------------------------------------------------------------


def test_cli_inspect_returns_rule_body() -> None:
    """inspect --rule-id 返回 success=True + 含 body."""
    result = _run_cli("inspect", "--rule-id", "rx-aminoglycoside-pediatric")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["rule"]["rule_id"] == "rx-aminoglycoside-pediatric"
    assert "body" in payload
    assert "氨基糖苷" in payload["body"]


def test_cli_inspect_no_body_excludes_body_field() -> None:
    """inspect --no-body 仅返回 summary,无 body 字段."""
    result = _run_cli(
        "inspect",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--no-body",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert "body" not in payload


def test_cli_inspect_rule_not_found_exits_nonzero() -> None:
    """不存在的 rule_id → stderr 报错 + 退出非零."""
    result = _run_cli("inspect", "--rule-id", "rx-does-not-exist")
    assert result.returncode != 0
    assert "未找到" in result.stderr or "error" in result.stderr


# ---------------------------------------------------------------------------
# load 子命令
# ---------------------------------------------------------------------------


def test_cli_load_returns_evidence_excerpt() -> None:
    """load --rule-id --order-context 返回 evidence_excerpt + order_context."""
    order = json.dumps({"drug_name": "庆大霉素", "patient_age": 8}, ensure_ascii=False)
    result = _run_cli("load", "--rule-id", "rx-aminoglycoside-pediatric", "--order-context", order)
    assert result.returncode == 0, f"stderr={result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["rule"]["rule_id"] == "rx-aminoglycoside-pediatric"
    assert payload["order_context"]["drug_name"] == "庆大霉素"
    assert isinstance(payload["evidence_excerpt"], str)
    assert len(payload["evidence_excerpt"]) > 0


def test_cli_load_invalid_order_context_exits_nonzero() -> None:
    """非 JSON 字符串 → 退出非零."""
    result = _run_cli(
        "load",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        "not-json",
    )
    assert result.returncode != 0
    assert "JSON" in result.stderr


# ---------------------------------------------------------------------------
# apply 子命令
# ---------------------------------------------------------------------------


def test_cli_apply_missing_operator_exits_nonzero() -> None:
    """apply 缺 --operator → argparse 拒绝 (exit 2)."""
    result = _run_cli(
        "apply",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        '{"drug_name":"庆大霉素"}',
    )
    assert result.returncode != 0


def test_cli_apply_happy_path_appends_audit() -> None:
    """apply 合法 args → confirmed=False payload + audit.jsonl 追加 1 行."""
    audit_path = PROJECT_ROOT / "audit" / "test-audit.jsonl"
    if audit_path.exists():
        audit_path.unlink()
    order = json.dumps(
        {"drug_name": "庆大霉素", "patient_age": 8, "egfr": 90},
        ensure_ascii=False,
    )
    result = _run_cli(
        "apply",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        order,
        "--operator",
        "pharmacist-001",
        "--audit-path",
        str(audit_path),
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    applied = payload["applied"]
    assert applied["rule_id"] == "rx-aminoglycoside-pediatric"
    assert applied["operator"] == "pharmacist-001"
    assert applied["confirmed"] is False
    assert len(applied["order_hash"]) == 64
    assert len(applied["session_id"]) == 16
    assert "applied_at" in applied
    assert isinstance(applied["evidence_excerpt"], str)

    # audit.jsonl 应追加 1 行 + confirmed=False
    assert audit_path.exists()
    lines = [
        line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 1
    audit_event = json.loads(lines[0])
    assert audit_event["confirmed"] is False
    assert audit_event["action"] == "apply"
    assert audit_event["operator"] == "pharmacist-001"
    assert audit_event["rule_id"] == "rx-aminoglycoside-pediatric"

    audit_path.unlink(missing_ok=True)


def test_cli_apply_idempotent_session_id() -> None:
    """同一 order_context + operator → 同 session_id (幂等)."""
    order = json.dumps(
        {"drug_name": "庆大霉素", "patient_age": 8},
        ensure_ascii=False,
    )
    r1 = _run_cli(
        "apply",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        order,
        "--operator",
        "pharmacist-001",
    )
    r2 = _run_cli(
        "apply",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        order,
        "--operator",
        "pharmacist-001",
    )
    assert r1.returncode == 0 and r2.returncode == 0
    s1 = json.loads(r1.stdout)["applied"]
    s2 = json.loads(r2.stdout)["applied"]
    assert s1["order_hash"] == s2["order_hash"]
    assert s1["session_id"] == s2["session_id"]


# ---------------------------------------------------------------------------
# audit-export 子命令
# ---------------------------------------------------------------------------


def test_cli_audit_export_writes_csv_with_bom(tmp_path: Path) -> None:
    """audit-export 生成 CSV + utf-8-sig BOM + 列头齐全 + 行数正确."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    # 写入两条当月 + 一条非当月事件
    sample = {
        "timestamp": "2026-09-08T10:00:00+00:00",
        "rule_id": "rx-aminoglycoside-pediatric",
        "action": "apply",
        "operator": "pharmacist-001",
        "session_id": "abc123",
        "order_hash": "h" * 64,
        "confirmed": False,
    }
    other_month = dict(sample, timestamp="2026-08-15T10:00:00+00:00")
    audit_path.write_text(
        json.dumps(sample, ensure_ascii=False) + "\n"
        + json.dumps(sample, ensure_ascii=False) + "\n"
        + json.dumps(other_month, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_csv = tmp_path / "out.csv"
    result = _run_cli(
        "audit-export",
        "--month",
        "2026-09",
        "--audit-path",
        str(audit_path),
        "--out",
        str(out_csv),
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert out_csv.exists()
    # utf-8-sig BOM
    assert out_csv.read_bytes()[:3] == b"\xef\xbb\xbf"
    # 列头 + 行数
    import csv as _csv
    with out_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(_csv.reader(fh))
    assert rows[0] == [
        "timestamp",
        "rule_id",
        "action",
        "operator",
        "session_id",
        "order_hash",
        "confirmed",
    ]
    assert len(rows) == 3  # header + 2 当月事件


def test_cli_audit_export_missing_file_exits_nonzero(tmp_path: Path) -> None:
    """audit log 不存在 → 退出非零 + stderr 报错."""
    missing = tmp_path / "no-such.jsonl"
    result = _run_cli(
        "audit-export",
        "--month",
        "2026-09",
        "--audit-path",
        str(missing),
    )
    assert result.returncode != 0
    assert "audit log" in result.stderr


# ---------------------------------------------------------------------------
# index-build + help 文案
# ---------------------------------------------------------------------------


def test_cli_index_build_is_idempotent(tmp_path: Path) -> None:
    """index-build 多次跑结果一致 + 退出 0."""
    rules_dir = PROJECT_ROOT / "rules"
    out_path = tmp_path / "rules.json"
    r1 = _run_cli(
        "index-build",
        "--rules-dir",
        str(rules_dir),
        "--out",
        str(out_path),
        "--index",
        str(PROJECT_ROOT / "rules_index.json"),
    )
    assert r1.returncode == 0, f"stderr={r1.stderr}"
    rules_v1 = json.loads(out_path.read_text(encoding="utf-8"))["rules"]
    assert len(rules_v1) >= 12

    r2 = _run_cli(
        "index-build",
        "--rules-dir",
        str(rules_dir),
        "--out",
        str(out_path),
        "--index",
        str(PROJECT_ROOT / "rules_index.json"),
    )
    assert r2.returncode == 0
    rules_v2 = json.loads(out_path.read_text(encoding="utf-8"))["rules"]
    assert [r["rule_id"] for r in rules_v1] == [r["rule_id"] for r in rules_v2]


def test_cli_help_lists_all_six_subcommands() -> None:
    """`pass-fc --help` 应列出 6 个子命令."""
    result = _run_cli("--help")
    assert result.returncode == 0
    for cmd in ("index-build", "search", "inspect", "load", "apply", "audit-export"):
        assert cmd in result.stdout, f"{cmd!r} missing from --help"


def test_cli_search_help_shows_options() -> None:
    """`pass-fc search --help` 应含 query / --drug-class / --limit / --json."""
    result = _run_cli("search", "--help")
    assert result.returncode == 0
    assert "query" in result.stdout
    assert "--drug-class" in result.stdout
    assert "--limit" in result.stdout
    assert "--json" in result.stdout


def test_cli_apply_help_requires_operator() -> None:
    """`pass-fc apply --help` 应显式提及 operator 必填语义."""
    result = _run_cli("apply", "--help")
    assert result.returncode == 0
    assert "--operator" in result.stdout
    assert "工号" in result.stdout


# ---------------------------------------------------------------------------
# Task 27: --no-color / --verbose / --human / 错误 JSON 化
# ---------------------------------------------------------------------------


def test_cli_search_default_has_severity_color_codes() -> None:
    """默认 search 输出含 severity 列 ANSI 颜色码 (high → 红).

    task 27: 默认开启颜色,severity=HIGH 应出现 ANSI RED escape 序列。
    """
    result = _run_cli("search", "庆大霉素 儿童")
    assert result.returncode == 0, f"stderr={result.stderr}"
    # ANSI 红色码 = ESC [31m
    assert "\033[31m" in result.stdout
    # 至少一处 HIGH severity 命中
    assert "HIGH" in result.stdout
    # 重置码
    assert "\033[0m" in result.stdout


def test_cli_search_no_color_disables_ansi_escapes() -> None:
    """--no-color 关闭所有 ANSI 颜色码 (输出纯文本).

    task 27: --no-color 全局参数关闭颜色,即便默认应该开启的场景下。
    """
    result = _run_cli("search", "--no-color", "庆大霉素 儿童")
    assert result.returncode == 0, f"stderr={result.stderr}"
    # 不应再含任何 ANSI escape
    assert "\033[" not in result.stdout
    # 但表格头与 HIGH 文本仍在
    assert "rule_id" in result.stdout
    assert "HIGH" in result.stdout


def test_cli_search_verbose_shows_query_tokens_and_top_n() -> None:
    """--verbose 输出 query_tokens / drug_class 过滤 / 命中数 / top-N rule_id+score.

    task 27: --verbose 便于规则维护者调试「为什么这条没命中」。
    """
    result = _run_cli("search", "--verbose", "庆大霉素 儿童")
    assert result.returncode == 0, f"stderr={result.stderr}"
    # [verbose] 前缀行
    assert "[verbose]" in result.stdout
    assert "query_tokens" in result.stdout
    # 命中数 + top-N 行
    assert "hits:" in result.stdout
    assert "rx-aminoglycoside-pediatric" in result.stdout
    # 表格仍渲染 (确保 verbose 不破坏默认 human 表格)
    assert "rule_id" in result.stdout


def test_cli_inspect_human_outputs_yaml_like() -> None:
    """inspect --human 输出 yaml-like (key: value 多行 + 不可解析为 JSON).

    task 27: --human 便于人眼查看,而不是程序化 JSON 解析。
    """
    result = _run_cli(
        "inspect",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--no-body",
        "--human",
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    # yaml-like 字段标记
    assert "rule_id:" in result.stdout
    assert "drug_class:" in result.stdout
    assert "severity:" in result.stdout
    assert "evidence_source:" in result.stdout
    # 不应是合法 JSON (顶层应为 yaml 而非 {)
    assert not result.stdout.lstrip().startswith("{")
    # 确保不是 JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_cli_load_human_outputs_yaml_like_with_evidence_excerpt() -> None:
    """load --human 输出 yaml-like 含 rule + order_context + evidence_excerpt 段."""
    order = json.dumps({"drug_name": "庆大霉素", "patient_age": 8}, ensure_ascii=False)
    result = _run_cli(
        "load",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        order,
        "--human",
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "rule_id:" in result.stdout
    assert "evidence_excerpt:" in result.stdout
    assert "order_context:" in result.stdout


def test_cli_apply_human_outputs_yaml_like_with_confirmed_false() -> None:
    """apply --human 输出 yaml-like 含 applied 段 + confirmed=false (安全边界)."""
    order = json.dumps({"drug_name": "庆大霉素", "patient_age": 8}, ensure_ascii=False)
    result = _run_cli(
        "apply",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        order,
        "--operator",
        "pharmacist-task27-001",
        "--human",
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "applied:" in result.stdout
    # 安全边界:本工具永不代签
    assert "confirmed: false" in result.stdout
    assert "order_hash:" in result.stdout
    assert "session_id:" in result.stdout
    assert "pharmacist-task27-001" in result.stdout


def test_cli_error_output_is_structured_json_to_stderr() -> None:
    """错误统一走 JSON: inspect 不存在 rule_id → exit 1 + stderr 是合法 JSON envelope.

    task 27: 错误响应格式 {success:false, error:{code, message}},便于 shell/CI
    解析,且 message 保留人类可读字符串。
    """
    result = _run_cli("inspect", "--rule-id", "rx-does-not-exist")
    assert result.returncode != 0
    # 退出码 1 (task 27 统一约定)
    assert result.returncode == 1
    # stderr 应是合法 JSON envelope
    err = json.loads(result.stderr.strip().splitlines()[-1])
    assert err["success"] is False
    assert err["error"]["code"] == "RULE_NOT_FOUND"
    assert "未找到" in err["error"]["message"]
    assert "rx-does-not-exist" in err["error"]["message"]


def test_cli_error_output_is_json_for_invalid_order_context() -> None:
    """load 非法 --order-context → JSON envelope + INVALID_INPUT code."""
    result = _run_cli(
        "load",
        "--rule-id",
        "rx-aminoglycoside-pediatric",
        "--order-context",
        "not-json",
    )
    assert result.returncode != 0
    err = json.loads(result.stderr.strip().splitlines()[-1])
    assert err["success"] is False
    assert err["error"]["code"] == "INVALID_INPUT"
    # message 保留 'JSON' 关键词,便于既有 grep 兼容
    assert "JSON" in err["error"]["message"]


def test_cli_error_output_is_json_for_missing_audit_file() -> None:
    """audit-export 不存在 audit.jsonl → JSON envelope + INVALID_INPUT."""
    result = _run_cli(
        "audit-export",
        "--month",
        "2026-09",
        "--audit-path",
        "/tmp/no-such-audit-task27.jsonl",
    )
    assert result.returncode != 0
    err = json.loads(result.stderr.strip().splitlines()[-1])
    assert err["success"] is False
    assert err["error"]["code"] == "INVALID_INPUT"
    assert "audit log" in err["error"]["message"]


def test_cli_help_lists_no_color_and_verbose() -> None:
    """`pass-fc --help` 应含 --no-color 与 --verbose 描述 (task 27 全局参数)."""
    result = _run_cli("--help")
    assert result.returncode == 0
    assert "--no-color" in result.stdout
    assert "--verbose" in result.stdout


def test_cli_inspect_help_lists_human_flag() -> None:
    """`pass-fc inspect --help` 应含 --human 描述 (task 27)."""
    result = _run_cli("inspect", "--help")
    assert result.returncode == 0
    assert "--human" in result.stdout
    assert "yaml" in result.stdout.lower()

# ---------------------------------------------------------------------------
# audit-summary 子命令 (task 31)
# ---------------------------------------------------------------------------


def _seed_audit_jsonl(path: Path) -> str:
    """写一份跨两月的 audit.jsonl,返回本月(2026-09)月份串。

    脱敏说明:operator 为示意工号占位,不对应任何真实人员。
    """
    rows = [
        # 2026-09:4 条 apply(1 条已确认)+ 1 条 search
        ("2026-09-05T09:00:00+00:00", "rx-aminoglycoside-pediatric", "apply", "pharm-a", True),
        ("2026-09-05T09:30:00+00:00", "rx-aminoglycoside-pediatric", "apply", "pharm-a", False),
        ("2026-09-06T10:00:00+00:00", "rx-nsaid-pregnancy", "apply", "pharm-b", False),
        ("2026-09-07T11:00:00+00:00", "rx-ppi-longterm", "apply", "pharm-b", False),
        ("2026-09-07T11:05:00+00:00", "rx-ppi-longterm", "search", "pharm-b", False),
        # 2026-08:不应进入本月简报
        ("2026-08-30T09:00:00+00:00", "rx-warfarin-inr", "apply", "pharm-c", False),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ts, rule_id, action, operator, confirmed in rows:
            fh.write(
                json.dumps(
                    {
                        "timestamp": ts,
                        "rule_id": rule_id,
                        "action": action,
                        "operator": operator,
                        "confirmed": confirmed,
                        "order_hash": "0" * 64,
                        "session_id": "1" * 16,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return "2026-09"


def test_cli_audit_summary_markdown(tmp_path: Path) -> None:
    """audit-summary --month → markdown 简报含表头 + 高频规则 + 确认率。"""
    audit_path = tmp_path / "audit.jsonl"
    month = _seed_audit_jsonl(audit_path)
    result = _run_cli(
        "audit-summary", "--month", month, "--audit-path", str(audit_path)
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    out = result.stdout
    # markdown 表头
    assert "| 排名 | 规则 | 严重度 | 命中次数 | 占比 |" in out
    assert "| 操作者 | 事件数 | 占比 |" in out
    assert "| 日期 | 事件数 | 分布 |" in out
    # 章节
    assert "## 高频命中规则" in out
    assert "## 严重度分布" in out
    # 数据:命中最多的规则排第一;上月规则不出现
    assert "rx-aminoglycoside-pediatric" in out
    assert "rx-warfarin-inr" not in out
    # 确认率 1/4 = 25.0%
    assert "25.0%" in out


def test_cli_audit_summary_json(tmp_path: Path) -> None:
    """audit-summary --json → 原始聚合 JSON,字段齐全且只含本月事件。"""
    audit_path = tmp_path / "audit.jsonl"
    month = _seed_audit_jsonl(audit_path)
    result = _run_cli(
        "audit-summary",
        "--month",
        month,
        "--audit-path",
        str(audit_path),
        "--json",
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    data = json.loads(result.stdout)
    assert data["month"] == month
    assert data["total_events"] == 5  # 上月 1 条被过滤
    assert data["apply_count"] == 4
    assert data["confirmed_count"] == 1
    assert data["confirmation_rate"] == 0.25
    assert data["top_triggered_rules"][0]["rule_id"] == "rx-aminoglycoside-pediatric"
    assert data["top_triggered_rules"][0]["count"] == 2
    # severity 由 data/rules.json join 而来(模块 fixture 已 index-build)
    assert data["severity_joined"] is True
    assert data["top_triggered_rules"][0]["severity"] == "high"
    ops = {i["key"] for i in data["operator_distribution"]}
    assert ops == {"pharm-a", "pharm-b"}


def test_cli_audit_summary_out_file(tmp_path: Path) -> None:
    """audit-summary --out → 简报写入文件,stdout 只报路径。"""
    audit_path = tmp_path / "audit.jsonl"
    month = _seed_audit_jsonl(audit_path)
    out_md = tmp_path / "report" / "brief.md"
    result = _run_cli(
        "audit-summary",
        "--month",
        month,
        "--audit-path",
        str(audit_path),
        "--out",
        str(out_md),
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert out_md.exists()
    text = out_md.read_text(encoding="utf-8")
    assert "## 高频命中规则" in text
    assert "ok: 写入" in result.stdout


def test_cli_audit_summary_top_n(tmp_path: Path) -> None:
    """--top-n 1 → 高频规则表只保留 1 行。"""
    audit_path = tmp_path / "audit.jsonl"
    month = _seed_audit_jsonl(audit_path)
    result = _run_cli(
        "audit-summary",
        "--month",
        month,
        "--audit-path",
        str(audit_path),
        "--top-n",
        "1",
        "--json",
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert len(data["top_triggered_rules"]) == 1
    assert data["distinct_rules"] == 3  # 聚合口径不受展示截断影响


def test_cli_audit_summary_bad_month_exits_nonzero(tmp_path: Path) -> None:
    """非法 --month → exit 1 + INVALID_INPUT JSON envelope。"""
    audit_path = tmp_path / "audit.jsonl"
    _seed_audit_jsonl(audit_path)
    result = _run_cli(
        "audit-summary", "--month", "2026/09", "--audit-path", str(audit_path)
    )
    assert result.returncode == 1
    err = json.loads(result.stderr.strip().splitlines()[-1])
    assert err["success"] is False
    assert err["error"]["code"] == "INVALID_INPUT"


def test_cli_audit_summary_missing_audit_file_is_empty_brief(tmp_path: Path) -> None:
    """audit.jsonl 不存在 → exit 0 + 无留痕提示(不阻塞月度出报)。"""
    result = _run_cli(
        "audit-summary",
        "--month",
        "2026-09",
        "--audit-path",
        str(tmp_path / "nope.jsonl"),
    )
    assert result.returncode == 0
    assert "无审方留痕" in result.stdout


def test_cli_help_lists_audit_summary() -> None:
    """`pass-fc --help` 应列出 audit-summary 子命令。"""
    result = _run_cli("--help")
    assert result.returncode == 0
    assert "audit-summary" in result.stdout
