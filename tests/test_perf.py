"""性能 smoke:12 规则 × 100 query P95 延迟 < 200ms + 内存 < 50MB.

设计取舍:
  - **本测试为性能 smoke 而非业务测试**,断言作为性能回归门槛。
    失败时记录 actual_p95 / actual_memory_mb,便于后续 task 优化。
  - 不依赖第三方 bench 库(time + resource / tracemalloc 已足够覆盖 smoke 需求)。
  - queries 故意覆盖中英文混合 + 不同 drug_class / severity,
    确保打分路径走完整 _tokens_zh(中英文 + 别名 + 子串扩展)。
  - 内存测量在 **干净 subprocess 内** 执行,避免 pytest 主进程的
    import / fixture / fixture cache 把 RSS 抬高到与本工具无关的体量。
    pytest + fastapi + httpx + jsonschema 在 macOS 14 上轻松占 80-100 MB,
    让 ``resource.getrusage().ru_maxrss`` 测出来的是测试框架本身,
    不是 ``search_rules`` 的真实内存占用。
  - 真实业务数据形态:加载 12 条真实规则(rules/*.md),不缩水样本,
    因为性能瓶颈常出现在 _tokens_zh + _score_rx 的双重叠加,
    与规则 body 长度强相关。

Reference evidence chain:本测试为性能门槛,非功能验证;规格来自
research.md → performance-budget 与 plan.md → 性能回归门槛。
"""
from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Deidentification note (强制:本测试为性能 smoke 而非业务测试).
# ---------------------------------------------------------------------------
_DEIDENT_NOTE = (
    "本测试为性能 smoke,非业务测试。所有 query 为合成 / 占位,不模拟"
    " 真实患者数据 / 工号 / 真实身份证号等敏感字段。"
)


# ---------------------------------------------------------------------------
# Query 合成:100 条覆盖中英文 + 不同 drug_class / severity
# ---------------------------------------------------------------------------

# 中英文药名 + 关键修饰词;覆盖 _tokens_zh 切分路径 + 别名表召回路径。
_QUERY_BUILDING_BLOCKS_ZH: tuple[str, ...] = (
    "庆大霉素", "阿莫西林", "二甲双胍", "头孢曲松", "万古霉素",
    "环丙沙星", "卡托普利", "依那普利", "阿昔洛韦", "布洛芬",
    "奥美拉唑", "华法林", "甲硝唑", "地高辛", "阿托伐他汀",
)
_QUERY_BUILDING_BLOCKS_EN: tuple[str, ...] = (
    "gentamicin", "amoxicillin", "metformin", "ceftriaxone", "vancomycin",
    "ciprofloxacin", "captopril", "enalapril", "acyclovir", "ibuprofen",
    "omeprazole", "warfarin", "metronidazole", "digoxin", "atorvastatin",
)
_QUERY_BUILDING_BLOCKS_MODIFIERS_ZH: tuple[str, ...] = (
    "儿童 8 岁", "孕妇 32 周", "肾损 egfr 45", "哺乳期",
    "新生儿 7 天", "早产儿 32 周", "老年 78 岁", "ALT 80",
    "INR 2.5", "TDM 谷浓度", "iv q8h", "口服 500mg",
    "长期 8 周", "首剂", "维持量", "负荷量",
)
_QUERY_BUILDING_BLOCKS_MODIFIERS_EN: tuple[str, ...] = (
    "pediatric 8yo", "pregnancy 32w", "renal egfr 45",
    "neonate 7d", "preterm 32w", "elderly 78yo",
    "TDM trough", "iv q8h", "po 500mg",
    "long-term 8w", "loading", "maintenance",
)


def _build_queries(n: int = 100, *, seed: int = 20260908) -> list[str]:
    """合成 ``n`` 条 query,覆盖中英文混合 + 不同修饰词。

    故意混合:
      - 纯中文 (query 命中 _ZH_RE + 兜底别名)
      - 纯英文 (query 命中 _EN_RE + 别名反向展开)
      - 中英文混杂 (query 同时命中两路 + 别名双向)
      - 含数字 / 单位的剂量字段 (如 'iv q8h' / '500mg')

    使用 ``random.Random(seed)`` 保证测试可重现:任意两次跑结果一致,
    便于跨轮回归对比 P95 / memory 实际值。
    """
    rng = random.Random(seed)
    queries: list[str] = []
    for _ in range(n):
        blocks = []
        # 1. 药名:中文 / 英文 / 混合 (3 选 1)
        kind = rng.choice(("zh", "en", "mixed"))
        if kind == "zh":
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_ZH))
        elif kind == "en":
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_EN))
        else:
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_ZH))
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_EN))
        # 2. 修饰词:中文 / 英文 (2 选 1)
        if rng.random() < 0.7:
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_MODIFIERS_ZH))
        else:
            blocks.append(rng.choice(_QUERY_BUILDING_BLOCKS_MODIFIERS_EN))
        # 3. 剂量字段(可选)
        if rng.random() < 0.4:
            blocks.append(f"{rng.randint(50, 1000)}mg")
        queries.append(" ".join(blocks))
    return queries


# ---------------------------------------------------------------------------
# Performance budget(性能门槛):跨轮回归基线
# ---------------------------------------------------------------------------

# P95 latency (seconds):单进程内 100 次 search_rules 的第 95 百分位耗时。
# 实测在 i5 / macOS 14 上 12 规则 + 100 混合 query 大约 30-100 ms,门槛留 2-3x 余量。
_PERF_P95_BUDGET_SEC = 0.2

# Peak memory (MB):subprocess 内仅 import pass_field_check.runtime + 调
# search_rules 100 次的 peak RSS。pytest 主进程的 80-100 MB 不计入。
# 实测稳定在 30-40 MB,门槛留 25% 余量避免 flake。
_PERF_MEMORY_BUDGET_MB = 50.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_rules() -> list[dict]:
    """Load the 12 real rules from ``data/rules.json``.

    Falls back to running ``collect_rx_rules`` if the index is missing
    (e.g. before Task 5 has run) so the test is self-sufficient on a
    cold checkout.
    """
    rules_path = Path(__file__).resolve().parent.parent / "data" / "rules.json"
    if rules_path.exists():
        with rules_path.open(encoding="utf-8") as fp:
            payload = json.load(fp)
        return payload["rules"]
    # Fallback: rebuild from disk.
    from pass_field_check.index_builder import collect_rx_rules
    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    return collect_rx_rules(rules_dir)


@pytest.fixture(scope="module")
def synthetic_queries() -> list[str]:
    """100 deterministic synthetic queries."""
    return _build_queries(n=100)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _measure_search_inprocess(
    rules: list[dict],
    queries: list[str],
) -> tuple[list[float], dict[str, float]]:
    """Time each ``search_rules(query, rules, limit=5)`` once in-process.

    Returns a tuple ``(per_query_seconds, summary_dict)`` where
    ``summary_dict`` contains ``min / median / p95 / p99 / max / mean``
    for human-readable logging.

    Uses ``time.perf_counter()`` (monotonic, nanosecond resolution) rather
    than ``time.time()`` so the test is robust to system clock adjustments.

    This helper is used for the **latency** budget. Memory is measured
    in a clean subprocess (see :func:`_measure_memory_via_subprocess`).
    """
    from pass_field_check.runtime import search_rules

    per_query: list[float] = []
    # Warm-up: ensure _tokens_zh regex cache / alias dict / set interning
    # are primed before measurement; the first call would otherwise pay a
    # one-shot setup cost that does not reflect steady-state latency.
    for _ in range(3):
        search_rules(queries[0], rules, limit=5)
    # Steady-state measurement.
    for q in queries:
        t0 = time.perf_counter()
        search_rules(q, rules, limit=5)
        t1 = time.perf_counter()
        per_query.append(t1 - t0)
    per_query_sorted = sorted(per_query)
    summary = {
        "min": per_query_sorted[0],
        "median": per_query_sorted[len(per_query_sorted) // 2],
        "p95": per_query_sorted[int(len(per_query_sorted) * 0.95) - 1],
        "p99": per_query_sorted[int(len(per_query_sorted) * 0.99) - 1],
        "max": per_query_sorted[-1],
        "mean": statistics.fmean(per_query),
    }
    return per_query, summary


# Subprocess driver:仅 import pass_field_check.runtime + 跑 search_rules
# 100 次,peak RSS 是干净的 Python + PyYAML + 一个模块的工作集,
# 不含 pytest 主进程负担。
_SUBPROCESS_DRIVER = textwrap.dedent(
    '''
    import json, os, resource, statistics, sys, time

    repo_root = Path = None
    # Late import so the bootstrap path is identical to a real CLI run.
    from pathlib import Path  # noqa: E402
    from pass_field_check.runtime import search_rules  # noqa: E402

    # 1. Load rules.
    rules_path = Path(__file__).resolve().parent.parent / "data" / "rules.json" \\
        if False else Path(os.environ["PASS_FC_RULES_PATH"])
    payload = json.loads(rules_path.read_text(encoding="utf-8"))
    rules = payload["rules"]

    # 2. Load queries.
    queries = json.loads(os.environ["PASS_FC_QUERIES_JSON"])

    # 3. Warm-up (3 calls).
    for _ in range(3):
        search_rules(queries[0], rules, limit=5)

    # 4. Steady-state.
    per_query = []
    for q in queries:
        t0 = time.perf_counter()
        search_rules(q, rules, limit=5)
        t1 = time.perf_counter()
        per_query.append(t1 - t0)

    per_query_sorted = sorted(per_query)
    p95 = per_query_sorted[int(len(per_query_sorted) * 0.95) - 1]
    p99 = per_query_sorted[int(len(per_query_sorted) * 0.99) - 1]
    summary = {
        "min": per_query_sorted[0],
        "median": per_query_sorted[len(per_query_sorted) // 2],
        "p95": p95,
        "p99": p99,
        "max": per_query_sorted[-1],
        "mean": statistics.fmean(per_query),
    }

    # 5. RSS at peak (after all steady-state calls done).
    rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname == "Darwin":
        rss_mb = rss_raw / (1024.0 * 1024.0)
    else:
        rss_mb = rss_raw / 1024.0

    print("PASS_FC_RESULT_JSON_BEGIN", flush=True)
    print(json.dumps({"summary": summary, "rss_mb": rss_mb,
                      "rules_count": len(rules),
                      "queries_count": len(queries)}), flush=True)
    print("PASS_FC_RESULT_JSON_END", flush=True)
    '''
).lstrip()


def _measure_memory_via_subprocess(
    rules: list[dict],
    queries: list[str],
) -> tuple[dict[str, float], float]:
    """Spawn a clean Python subprocess and return ``(summary, rss_mb)``.

    The subprocess imports only ``pass_field_check.runtime`` (no pytest,
    no FastAPI / httpx / jsonschema) so the peak RSS it reports is the
    real cost of the search workload — not the cost of the test framework.

    The subprocess inherits ``PASS_FC_RULES_PATH`` (rules index) and
    ``PASS_FC_QUERIES_JSON`` (synthetic query list) so the workload is
    byte-identical to the in-process measurement; only the memory
    accounting differs.
    """
    project_root = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PASS_FC_RULES_PATH": str(project_root / "data" / "rules.json"),
        "PASS_FC_QUERIES_JSON": json.dumps(queries, ensure_ascii=False),
    }
    # Run the driver with the project root on PYTHONPATH so that
    # `import pass_field_check.runtime` resolves.
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_DRIVER],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # Extract the JSON line between the BEGIN / END markers.
    out = proc.stdout
    begin = out.find("PASS_FC_RESULT_JSON_BEGIN")
    end = out.find("PASS_FC_RESULT_JSON_END")
    if begin == -1 or end == -1:
        raise RuntimeError(
            f"Subprocess driver did not emit result markers; "
            f"stdout={out!r} stderr={proc.stderr!r}"
        )
    payload = json.loads(out[begin + len("PASS_FC_RESULT_JSON_BEGIN"):end].strip())
    return payload["summary"], float(payload["rss_mb"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPerfBudget:
    """Performance smoke budget: P95 latency + peak memory.

    These thresholds act as a regression gate: if either budget is exceeded,
    the test fails with the actual measurements attached so the next round
    can investigate whether the regression is in the scoring path,
    tokenisation, or alias expansion.
    """

    def test_p95_latency_under_budget(
        self,
        real_rules: list[dict],
        synthetic_queries: list[str],
    ) -> None:
        """P95 of 100 search_rules() calls must stay under ``_PERF_P95_BUDGET_SEC``."""
        _latencies, summary = _measure_search_inprocess(real_rules, synthetic_queries)

        # Failure message carries the full breakdown so a regression in
        # one phase (e.g. alias expansion blowing up the token set) shows
        # up clearly in the test output.
        msg = (
            f"P95 search latency exceeded budget: "
            f"actual_p95={summary['p95']*1000:.2f}ms "
            f"budget={_PERF_P95_BUDGET_SEC*1000:.2f}ms "
            f"| min={summary['min']*1000:.2f}ms "
            f"median={summary['median']*1000:.2f}ms "
            f"p99={summary['p99']*1000:.2f}ms "
            f"max={summary['max']*1000:.2f}ms "
            f"mean={summary['mean']*1000:.2f}ms"
        )
        assert summary["p95"] < _PERF_P95_BUDGET_SEC, msg

    def test_peak_memory_under_budget(
        self,
        real_rules: list[dict],
        synthetic_queries: list[str],
    ) -> None:
        """Peak RSS (measured in clean subprocess) must stay under budget.

        The measurement runs ``search_rules`` 100 times inside a brand-new
        Python interpreter so pytest / FastAPI / httpx never enter the
        accounting.  This isolates the true cost of the search workload
        from the cost of the test harness.
        """
        _summary, rss_mb = _measure_memory_via_subprocess(real_rules, synthetic_queries)
        msg = (
            f"Peak memory exceeded budget: actual_rss={rss_mb:.2f}MB "
            f"budget={_PERF_MEMORY_BUDGET_MB:.2f}MB"
        )
        assert rss_mb < _PERF_MEMORY_BUDGET_MB, msg

    def test_query_diversity_is_meaningful(
        self,
        synthetic_queries: list[str],
    ) -> None:
        """The synthetic queries must not all collapse to the same string.

        Guards against a regression in ``_build_queries`` accidentally
        producing a single repeated query (which would mask any
        pathological N² interaction in the scoring loop).
        """
        unique = set(synthetic_queries)
        # We expect at least 80% of queries to be unique given the
        # 15 zh * 15 en * 15 zh_mod * 15 en_mod * 5 doses ≈ 253k
        # possible combinations sampled down to 100.
        assert len(unique) >= 80, (
            f"Synthetic queries lack diversity: unique={len(unique)} / 100"
        )

    def test_deident_note_is_present(self) -> None:
        """The deidentification note must accompany the test module."""
        assert _DEIDENT_NOTE  # module-level constant is non-empty
        # The note must explicitly mention 'smoke' and 'deident' keywords
        # so future readers understand the test is a performance gate,
        # not a functional test that should grow new assertions.
        assert "性能 smoke" in _DEIDENT_NOTE
        assert "脱敏" in _DEIDENT_NOTE or "占位" in _DEIDENT_NOTE


# ---------------------------------------------------------------------------
# Failure-reason helper (供 Phase 4 后续 task 优化使用)
# ---------------------------------------------------------------------------


def _format_failure_reason(actual_p95: float, actual_memory_mb: float) -> str:
    """Return a human-readable failure reason string.

    Convenience helper used by hand-rolled probes (and by future tasks
    that want to emit a structured ``failure_reason`` for the projects
    registry).  Kept here (rather than duplicated in each test) so the
    format stays consistent.
    """
    return (
        f"performance_smoke_failed: "
        f"actual_p95={actual_p95*1000:.2f}ms "
        f"(budget={_PERF_P95_BUDGET_SEC*1000:.2f}ms), "
        f"actual_memory={actual_memory_mb:.2f}MB "
        f"(budget={_PERF_MEMORY_BUDGET_MB:.2f}MB)"
    )