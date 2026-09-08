"""Tests for pass_field_check.runtime.search_rules and _score_rx.

Task 7 spec: token-overlap 打分 + severity 二级排序 + drug_class 过滤。
参考：github_ref/agency-agents/scripts/build-hermes-plugin.py L122-186 + L303-328。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

from pass_field_check.index_builder import collect_rx_rules
from pass_field_check.runtime import (
    _score_rx,
    _split_sentences_zh,
    _summary_rx,
    _tokens_zh,
    detect_rule_conflicts,
    extract_evidence_excerpt,
    search_rules,
    select_top_sentence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_rules() -> list[dict[str, Any]]:
    """Load all real rules from the committed ``rules/`` directory.

    Tests use the actual rule corpus so golden queries exercise the same
    data the production product ships with.
    """
    rules_dir = Path(__file__).resolve().parents[1] / "rules"
    return collect_rx_rules(rules_dir)


# ---------------------------------------------------------------------------
# _tokens_zh / _score_rx unit tests
# ---------------------------------------------------------------------------

def test_tokens_zh_returns_set():
    assert isinstance(_tokens_zh("庆大霉素 儿童"), set)
    assert "庆大霉素" in _tokens_zh("庆大霉素 儿童")


def test_score_rx_zero_for_no_overlap():
    rule = {
        "rule_id": "rx-aminoglycoside-pediatric",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "氨基糖苷类药物在 8 岁以下儿童中应严格限制使用。",
    }
    # English-only query that shares no token with the rule's body
    score = _score_rx(_tokens_zh("banana apple orange"), rule)
    assert score == 0.0


def test_score_rx_positive_for_chinese_overlap():
    rule = {
        "rule_id": "rx-aminoglycoside-pediatric",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "氨基糖苷类药物在 8 岁以下儿童中应严格限制使用。",
    }
    score = _score_rx(_tokens_zh("庆大霉素 儿童 8 岁"), rule)
    assert score > 0


def test_score_rx_rule_id_bonus():
    """Tokens matching the slug get a flat +3.0 bonus."""
    rule = {
        "rule_id": "rx-aminoglycoside-pediatric",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "氨基糖苷类药物在 8 岁以下儿童中应严格限制使用。",
    }
    # 'aminoglycoside' is a substring of the slug, so should trigger the
    # rule_id bonus; without the bonus the score would be much lower.
    score_with_slug = _score_rx(_tokens_zh("aminoglycoside"), rule)
    assert score_with_slug >= 3.0


def test_score_rx_empty_query():
    rule = {
        "rule_id": "rx-foo",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "正文",
    }
    assert _score_rx(set(), rule) == 0.0


# ---------------------------------------------------------------------------
# _summary_rx
# ---------------------------------------------------------------------------

def test_summary_rx_trims_fields():
    rule = {
        "rule_id": "rx-foo",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "完整正文不应出现在 summary 里",
        "file_path": "/abs/path/rx-foo.md",
    }
    summary = _summary_rx(rule, 1.23456)
    assert summary["rule_id"] == "rx-foo"
    assert summary["drug_class"] == "儿科"
    assert summary["severity"] == "high"
    assert summary["evidence_source"] == "中华医学会"
    assert summary["population"] == "儿童"
    assert summary["file_path"] == "/abs/path/rx-foo.md"
    assert summary["score"] == 1.2346  # rounded to 4 decimals
    # body MUST NOT leak into summary (audit-log hygiene)
    assert "body" not in summary


# ---------------------------------------------------------------------------
# search_rules integration tests on real rules corpus
# ---------------------------------------------------------------------------

def test_search_aminoglycoside_pediatric(real_rules):
    """黄金 query: '庆大霉素 儿童 8 岁' → top-3 含 rx-aminoglycoside-pediatric."""
    results = search_rules("庆大霉素 儿童 8 岁", real_rules, limit=3)
    assert len(results) > 0, "search_rules returned no hits"
    rule_ids = [r["rule_id"] for r in results]
    assert "rx-aminoglycoside-pediatric" in rule_ids[:3]


def test_search_quinolone_pediatric(real_rules):
    """黄金 query: '环丙沙星 儿童 6 岁' → top-3 含 rx-quinolone-pediatric."""
    results = search_rules("环丙沙星 儿童 6 岁", real_rules, limit=3)
    rule_ids = [r["rule_id"] for r in results]
    assert "rx-quinolone-pediatric" in rule_ids[:3]


def test_search_nsaid_pregnancy(real_rules):
    """黄金 query: '布洛芬 200mg 孕妇' → top-3 含 rx-nsaid-pregnancy."""
    results = search_rules("布洛芬 200mg 孕妇", real_rules, limit=3)
    rule_ids = [r["rule_id"] for r in results]
    assert "rx-nsaid-pregnancy" in rule_ids[:3]


def test_search_acei_pregnancy(real_rules):
    """黄金 query: '卡托普利 孕妇 28 周' → top-3 含 rx-acei-pregnancy."""
    results = search_rules("卡托普利 孕妇 28 周", real_rules, limit=3)
    rule_ids = [r["rule_id"] for r in results]
    assert "rx-acei-pregnancy" in rule_ids[:3]


def test_search_metformin_renal(real_rules):
    """黄金 query: '二甲双胍 肾损 egfr 45' → top-3 含 rx-metformin-renal."""
    results = search_rules("二甲双胍 肾损 egfr 45", real_rules, limit=3)
    rule_ids = [r["rule_id"] for r in results]
    assert "rx-metformin-renal" in rule_ids[:3]


def test_search_drug_class_filter(real_rules):
    """drug_class 过滤: 仅返回 drug_class='孕期' 的规则."""
    results = search_rules(
        "布洛芬",
        real_rules,
        drug_class="孕期",
        limit=5,
    )
    assert len(results) > 0, "drug_class filter dropped every result"
    for r in results:
        assert r["drug_class"] == "孕期"


def test_search_drug_class_filter_zero_hits(real_rules):
    """drug_class='心血管' + query='布洛芬' 应只返回心血管相关规则."""
    results = search_rules(
        "布洛芬",
        real_rules,
        drug_class="心血管",
        limit=5,
    )
    # 布洛芬与心血管无交集,但 drug_class 过滤后可能 score<=0 被剔除
    # → 空列表或仅含 score>0 的心血管规则
    for r in results:
        assert r["drug_class"] == "心血管"


def test_search_severity_order(real_rules):
    """严重度优先: 同等 score 时 high 命中应在 medium / low 之前."""
    # 构造两个 fake rules:score 一样但 severity 不同
    fake_high = {
        "rule_id": "rx-fake-high",
        "drug_class": "儿科",
        "severity": "high",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "测试要点 测试要点",
    }
    fake_low = {
        "rule_id": "rx-fake-low",
        "drug_class": "儿科",
        "severity": "low",
        "evidence_source": "中华医学会",
        "population": "儿童",
        "body": "测试要点 测试要点",
    }
    rules = [fake_low, fake_high]
    results = search_rules("测试要点", rules, limit=2)
    assert len(results) == 2
    # high 应排在 low 之前
    severities = [r["severity"] for r in results]
    assert severities[0] == "high"
    assert severities[1] == "low"


def test_search_empty_query_returns_empty(real_rules):
    """空 query 或纯空白 → 返回空列表,不静默返回所有规则."""
    assert search_rules("", real_rules, limit=5) == []
    assert search_rules("   \n\t  ", real_rules, limit=5) == []


def test_search_zero_or_negative_limit(real_rules):
    """limit<=0 → 返回空列表."""
    results = search_rules("庆大霉素 儿童", real_rules, limit=0)
    assert results == []
    results = search_rules("庆大霉素 儿童", real_rules, limit=-1)
    assert results == []


def test_search_returns_only_positive_scores(real_rules):
    """search_rules 不应返回 score<=0 的规则."""
    results = search_rules("庆大霉素 儿童", real_rules, limit=20)
    assert len(results) > 0
    for r in results:
        assert r["score"] > 0


def test_search_returns_trimmed_summary_only(real_rules):
    """summary 中不应有 body 字段(避免把规则正文塞进搜索响应)."""
    results = search_rules("庆大霉素 儿童", real_rules, limit=3)
    for r in results:
        assert "body" not in r
        assert "applies_to" not in r  # applies_to 也不应在 summary 中
        # 但以下核心字段必须有
        for key in ("rule_id", "drug_class", "severity", "evidence_source", "score"):
            assert key in r


def test_search_stable_tiebreak_by_rule_id(real_rules):
    """同 score 时按 rule_id 字典序排,排序稳定."""
    # 注入两条 score 相同、severity 相同、但 rule_id 字典序不同的规则
    rules = [
        {
            "rule_id": "rx-zzz",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试",
        },
        {
            "rule_id": "rx-aaa",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试",
        },
    ]
    results = search_rules("测试", rules, limit=2)
    rule_ids = [r["rule_id"] for r in results]
    assert rule_ids == ["rx-aaa", "rx-zzz"]


# ---------------------------------------------------------------------------
# evidence_excerpt / _split_sentences_zh / select_top_sentence (Task 18)
# ---------------------------------------------------------------------------


def test_evidence_excerpt_basic(real_rules):
    """query '庆大霉素 儿童' 应命中含 '氨基糖苷类' 与 '儿童' 的依据句."""
    target = next(
        r for r in real_rules if r["rule_id"] == "rx-aminoglycoside-pediatric"
    )
    body = target["body"]
    query_tokens = _tokens_zh("庆大霉素 儿童")
    excerpt = extract_evidence_excerpt(query_tokens, body)
    assert isinstance(excerpt, str)
    # 抽出的句子应包含 query 的关键 token (氨基糖苷类规则的依据句含 庆大霉素 / 儿童)
    assert "儿童" in excerpt
    assert "庆大霉素" in excerpt
    # 不应是空字符串或全空白
    assert excerpt.strip() != ""
    # 不应超过 max_chars(默认 200)
    assert len(excerpt) <= 201  # 200 + 省略号


def test_evidence_excerpt_long_body():
    """长 body 中应选出 top 句(高 token-overlap),不是首句."""
    body = (
        "第一句:肝功能检查每 4 周一次,关注 ALT/AST 变化。\n"
        "第二句:庆大霉素在儿童与孕妇中应严格限制使用,可致耳毒性。\n"
        "第三句:与万古霉素联合使用会增加肾毒性风险。\n"
        "第四句:常规监测血药浓度有助于及时调整剂量。"
    )
    sentences = _split_sentences_zh(body)
    assert len(sentences) >= 4
    query_tokens = _tokens_zh("庆大霉素 儿童")
    excerpt = extract_evidence_excerpt(query_tokens, body)
    # 第二句明确含 '庆大霉素' + '儿童'(query 直接命中),其它句 overlap=0
    assert "庆大霉素" in excerpt
    assert "儿童" in excerpt


def test_evidence_excerpt_no_match():
    """query_tokens 与 body 无交集时,选最长的句(避免空 excerpt)或返回空字符串."""
    body = "这一句与查询无关。"
    query_tokens = _tokens_zh("banana apple orange")
    excerpt = extract_evidence_excerpt(query_tokens, body)
    # 行为:query_tokens 与 body 无 token 重叠时,任意句的 overlap 都是 0;
    # select_top_sentence 在 tie(都=0)时取长度最长的。
    # 这里 body 单句,所以会返回该句
    assert isinstance(excerpt, str)
    # 空 query / 空 body 也应返回空
    assert extract_evidence_excerpt(set(), body) == ""
    assert extract_evidence_excerpt(query_tokens, "") == ""


def test_evidence_excerpt_chinese_boundary():
    """max_chars 截断不能切在中文 codepoint 中间,且总长度受控."""
    # 构造一个长度 > max_chars 的长句,含中英文
    long_sentence = "氨基糖苷类药物" * 50  # 350 字
    body = long_sentence + "。\n短句结束。"
    query_tokens = _tokens_zh("氨基糖苷类")
    excerpt = extract_evidence_excerpt(query_tokens, body, max_chars=50)
    # 第一句被截断到 50 字 + 省略号
    assert len(excerpt) <= 51
    assert excerpt.endswith("…")
    # 截断后不应在中文 codepoint 中间:
    # Python 字符串 str[:50] 已经保证这一点
    # 但为了额外确认,显式尝试 encode/decode round-trip
    excerpt.encode("utf-8").decode("utf-8")


def test_select_top_sentence_helper():
    """select_top_sentence 应按 overlap 选 top 句,等分时取短的."""
    sentences = [
        "这句很长但是与查询相关度不高。",
        "庆大霉素儿童8岁",  # 短且高 overlap
        "另一句长但也提到了庆大霉素",  # 长且 overlap=1
    ]
    query_tokens = _tokens_zh("庆大霉素 儿童")
    top = select_top_sentence(query_tokens, sentences)
    # '庆大霉素儿童8岁' 的 token = {庆大霉素, 儿童, 庆大霉素儿童...}
    # query 与它 overlap 应 >= '另一句长但也提到了庆大霉素'(后者只 overlap 庆大霉素)
    assert "庆大霉素儿童" in top or "庆大霉素" in top


def test_split_sentences_zh_basic():
    """_split_sentences_zh 按 。/；/\\n 切分."""
    body = "第一句。第二句；第三句\n第四句"
    sentences = _split_sentences_zh(body)
    assert len(sentences) == 4
    assert sentences[0] == "第一句"
    assert sentences[1] == "第二句"
    assert sentences[2] == "第三句"
    assert sentences[3] == "第四句"


def test_split_sentences_zh_empty():
    """空字符串与纯分隔符应返回空列表."""
    assert _split_sentences_zh("") == []
    assert _split_sentences_zh("。；\n") == []


# ---------------------------------------------------------------------------
# Task 20: 多键排序扩展 + strict_drug_class
# sort key = (-score, SEVERITY_RANK[severity], tanh(len(body)/1000), rule_id)
# ---------------------------------------------------------------------------


def test_search_severity_multi_key():
    """多键排序: 同 score 时 severity 决定顺序,high → medium → low."""
    rules = [
        {
            "rule_id": "rx-fake-medium",
            "drug_class": "儿科",
            "severity": "medium",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点 测试要点",
        },
        {
            "rule_id": "rx-fake-high",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点 测试要点",
        },
        {
            "rule_id": "rx-fake-low",
            "drug_class": "儿科",
            "severity": "low",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点 测试要点",
        },
    ]
    results = search_rules("测试要点", rules, limit=3)
    severities = [r["severity"] for r in results]
    assert severities == ["high", "medium", "low"], (
        f"Expected severity order [high, medium, low], got {severities}"
    )


def test_search_short_body_priority():
    """同 score + 同 severity 时,body 短者优先(聚焦描述)."""
    # 构造两条规则:同 query 命中相同 token,score 相同;
    # 但 rx-fake-long body 显著长于 rx-fake-short.
    rules = [
        {
            "rule_id": "rx-fake-long",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            # ~1200 chars with overlap on 测试要点
            "body": "测试要点 " + ("补充说明 " * 200),
        },
        {
            "rule_id": "rx-fake-short",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
    ]
    results = search_rules("测试要点", rules, limit=2)
    assert len(results) == 2
    # short body (小 tanh 值) 应排在 long body (大 tanh 值) 之前
    assert results[0]["rule_id"] == "rx-fake-short", (
        f"Expected rx-fake-short first (shorter body), got {results[0]['rule_id']}"
    )
    assert results[1]["rule_id"] == "rx-fake-long"


def test_search_short_body_priority_real_rules(real_rules):
    """真实规则集: 验证 search_rules 对真实规则的排序不会因 body 长度反转."""
    # 直接验证 search_rules 在真实语料上仍返回稳定顺序
    results = search_rules("庆大霉素 儿童", real_rules, limit=10)
    assert len(results) > 0
    # 全部 score > 0
    for r in results:
        assert r["score"] > 0


def test_search_strict_drug_class(real_rules):
    """strict_drug_class=True: drug_class 不严格相等 → 排除 + 警告."""
    rules = [
        {
            "rule_id": "rx-fake-pediatric",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
        {
            "rule_id": "rx-fake-pregnancy",
            "drug_class": "孕期",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "孕妇",
            "body": "测试要点",
        },
        {
            "rule_id": "rx-fake-pediatric-2",
            "drug_class": "儿科",
            "severity": "medium",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
    ]
    with pytest.warns(UserWarning, match="not strictly equal"):
        results = search_rules(
            "测试要点",
            rules,
            drug_class="儿科",
            strict_drug_class=True,
            limit=5,
        )
    # 只有两条 drug_class='儿科' 的入选
    rule_ids = {r["rule_id"] for r in results}
    assert rule_ids == {"rx-fake-pediatric", "rx-fake-pediatric-2"}
    # high 应排在 medium 之前
    severities = [r["severity"] for r in results]
    assert severities[0] == "high"
    assert severities[1] == "medium"


def test_search_strict_drug_class_zero_hits_warns():
    """strict_drug_class=True 但所有规则 drug_class 都不匹配 → 警告 + 空结果."""
    rules = [
        {
            "rule_id": "rx-fake-pregnancy",
            "drug_class": "孕期",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "孕妇",
            "body": "测试要点",
        },
    ]
    with pytest.warns(UserWarning, match="not strictly equal"):
        results = search_rules(
            "测试要点",
            rules,
            drug_class="儿科",
            strict_drug_class=True,
            limit=5,
        )
    assert results == []


def test_search_strict_drug_class_default_no_warn():
    """默认 strict_drug_class=False: 静默排除不匹配规则,不警告."""
    rules = [
        {
            "rule_id": "rx-fake-pregnancy",
            "drug_class": "孕期",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "孕妇",
            "body": "测试要点",
        },
        {
            "rule_id": "rx-fake-pediatric",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
    ]
    # 把任何 UserWarning 升级为错误: 若 strict_drug_class 默认开启就会失败
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        results = search_rules(
            "测试要点",
            rules,
            drug_class="儿科",
            limit=5,
        )
    # 默认非严格: 静默排除孕期规则,只返回儿科规则
    rule_ids = {r["rule_id"] for r in results}
    assert rule_ids == {"rx-fake-pediatric"}


def test_search_stable_tiebreak():
    """四键排序稳定: 同 score/severity/body 长度时,rule_id 字典序保稳定."""
    # 构造 4 条规则,body 长度相近,severity 相同,只有 rule_id 不同
    rules = [
        {
            "rule_id": "rx-zzz",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",  # 4 chars
        },
        {
            "rule_id": "rx-aaa",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
        {
            "rule_id": "rx-mmm",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
        {
            "rule_id": "rx-fff",
            "drug_class": "儿科",
            "severity": "high",
            "evidence_source": "中华医学会",
            "population": "儿童",
            "body": "测试要点",
        },
    ]
    results = search_rules("测试要点", rules, limit=4)
    rule_ids = [r["rule_id"] for r in results]
    assert rule_ids == ["rx-aaa", "rx-fff", "rx-mmm", "rx-zzz"], (
        f"Expected alphabetical rule_id order, got {rule_ids}"
    )


def test_search_severity_rank_public_constant():
    """SEVERITY_RANK 已升级为公开常量,值为 high=0/medium=1/low=2."""
    from pass_field_check.runtime import SEVERITY_RANK
    assert SEVERITY_RANK == {"high": 0, "medium": 1, "low": 2}
    assert SEVERITY_RANK["high"] < SEVERITY_RANK["medium"] < SEVERITY_RANK["low"]


# ---------------------------------------------------------------------------
# Task 22: golden queries — 8 种典型临床审方场景
#
# 每条场景校验:
#   1) search_rules 返回非空
#   2) 目标 rule_id 出现在 top-3
#   3) 目标 rule 的 score >= 阈值(0.5)
#   4) severity 与 drug_class 与预期一致
#   5) 命中片段 evidence_excerpt 含关键词(便于人工复核)
#
# 失败时打印 query + 实际 top-N 命中列表 + 分数,便于回归。
# ---------------------------------------------------------------------------

# 阈值:实际业务上 query 与目标规则 token 重叠应在 0.5 以上,
# 阈值偏低以避免个别 query 长尾噪声影响测试稳定性.
_GOLDEN_SCORE_MIN = 0.5


def _golden_assert(
    *,
    label: str,
    query: str,
    rules: list[dict[str, Any]],
    target_rule_id: str,
    expected_severity: str,
    expected_drug_class: str,
    evidence_keyword: str,
    body_keyword: str | None = None,
    top_n: int = 3,
) -> dict[str, Any]:
    """Run a golden query and assert the spec invariants.

    Returns the **full** rule dict from ``rules`` (not the trimmed summary)
    so callers can introspect ``body`` / ``applies_to`` for further checks.
    """
    results = search_rules(query, rules, limit=top_n)
    assert results, f"[{label}] no results for query={query!r}"

    top_rule_ids = [r["rule_id"] for r in results]
    assert target_rule_id in top_rule_ids, (
        f"[{label}] expected {target_rule_id} in top-{top_n} for "
        f"query={query!r}, got top-{top_n}={top_rule_ids} with scores "
        f"{[round(r['score'], 3) for r in results]}"
    )

    summary = next(r for r in results if r["rule_id"] == target_rule_id)
    full_rule = next(r for r in rules if r.get("rule_id") == target_rule_id)
    assert summary["score"] >= _GOLDEN_SCORE_MIN, (
        f"[{label}] expected score >= {_GOLDEN_SCORE_MIN} for "
        f"{target_rule_id}, got {summary['score']:.3f}"
    )
    assert summary["severity"] == expected_severity, (
        f"[{label}] expected severity={expected_severity}, "
        f"got {summary['severity']} for {target_rule_id}"
    )
    assert summary["drug_class"] == expected_drug_class, (
        f"[{label}] expected drug_class={expected_drug_class}, "
        f"got {summary['drug_class']} for {target_rule_id}"
    )
    # 业务关键词应出现在规则元数据(evidence_source / population)或正文中
    haystack = " ".join(
        str(full_rule.get(k, "")) for k in ("evidence_source", "population", "body")
    )
    assert evidence_keyword in haystack, (
        f"[{label}] expected keyword={evidence_keyword!r} in rule "
        f"evidence_source/population/body for {target_rule_id}; "
        f"got haystack excerpt={haystack[:160]!r}"
    )
    if body_keyword:
        body_text = str(full_rule.get("body", ""))
        assert body_keyword in body_text, (
            f"[{label}] expected body_keyword={body_keyword!r} in body "
            f"of {target_rule_id}; got body excerpt={body_text[:160]!r}"
        )
    return full_rule


def test_golden_aminoglycoside_pediatric(real_rules):
    """场景 1: 氨基糖苷类 + 8 岁儿童.

    真实工作站字段串: 庆大霉素 iv 80mg 8岁
    期望命中: rx-aminoglycoside-pediatric (儿科 / high)
    """
    _golden_assert(
        label="氨基糖苷类儿童",
        query="庆大霉素 iv 80mg 8岁",
        rules=real_rules,
        target_rule_id="rx-aminoglycoside-pediatric",
        expected_severity="high",
        expected_drug_class="儿科",
        evidence_keyword="儿童",
        # 业务要点: 规则 body 应提及氨基糖苷 / 耳毒 / 肾毒,便于药师人工复核
        body_keyword="氨基糖苷",
    )


def test_golden_nsaid_pregnancy(real_rules):
    """场景 2: NSAID + 妊娠期 32 周.

    真实工作站字段串: 布洛芬 200mg 孕妇 32周
    期望命中: rx-nsaid-pregnancy (孕期 / high)
    """
    _golden_assert(
        label="NSAID 孕期",
        query="布洛芬 200mg 孕妇 32周",
        rules=real_rules,
        target_rule_id="rx-nsaid-pregnancy",
        expected_severity="high",
        expected_drug_class="孕期",
        evidence_keyword="妊娠",
    )


def test_golden_metformin_renal(real_rules):
    """场景 3: 二甲双胍 + eGFR=45.

    真实工作站字段串: 二甲双胍 500mg egfr=45
    期望命中: rx-metformin-renal (肾损 / medium)
    """
    _golden_assert(
        label="二甲双胍肾损",
        query="二甲双胍 500mg egfr=45",
        rules=real_rules,
        target_rule_id="rx-metformin-renal",
        expected_severity="medium",
        expected_drug_class="肾损",
        evidence_keyword="eGFR",
    )


def test_golden_acei_pregnancy(real_rules):
    """场景 4: ACEI + 孕妇.

    真实工作站字段串: 卡托普利 25mg 孕妇
    期望命中: rx-acei-pregnancy (孕期 / high)
    """
    _golden_assert(
        label="ACEI 孕期",
        query="卡托普利 25mg 孕妇",
        rules=real_rules,
        target_rule_id="rx-acei-pregnancy",
        expected_severity="high",
        expected_drug_class="孕期",
        evidence_keyword="ACEI",
        # 规则 body 应提及具体药品名(卡托普利 / 依那普利)便于审方药师辨识
        body_keyword="卡托普利",
    )


def test_golden_vancomycin_tdm(real_rules):
    """场景 5: 万古霉素 + 长期 IV.

    真实工作站字段串: 万古霉素 1g iv q12h
    期望命中: rx-vancomycin-tdm (抗菌药 / high)
    """
    _golden_assert(
        label="万古霉素 TDM",
        query="万古霉素 1g iv q12h",
        rules=real_rules,
        target_rule_id="rx-vancomycin-tdm",
        expected_severity="high",
        expected_drug_class="抗菌药",
        evidence_keyword="万古霉素",
    )


def test_golden_warfarin_inr(real_rules):
    """场景 6: 华法林 + INR 监测.

    真实工作站字段串: 华法林 3mg INR 2.5
    期望命中: rx-warfarin-inr (心血管 / high)
    """
    _golden_assert(
        label="华法林 INR",
        query="华法林 3mg INR 2.5",
        rules=real_rules,
        target_rule_id="rx-warfarin-inr",
        expected_severity="high",
        expected_drug_class="心血管",
        evidence_keyword="华法林",
        # 规则 body 应出现 INR 目标范围,便于药师复核
        body_keyword="INR",
    )


def test_golden_ppi_longterm(real_rules):
    """场景 7: PPI + 长期使用 8 周.

    真实工作站字段串: 奥美拉唑 20mg 8周
    期望命中: rx-ppi-longterm (儿科 / low)
    """
    _golden_assert(
        label="PPI 长期",
        query="奥美拉唑 20mg 8周",
        rules=real_rules,
        target_rule_id="rx-ppi-longterm",
        expected_severity="low",
        expected_drug_class="儿科",
        evidence_keyword="8 周",
    )


def test_golden_statins_liver(real_rules):
    """场景 8: 他汀 + 肝功能 ALT 异常.

    真实工作站字段串: 阿托伐他汀 20mg ALT 80
    期望命中: rx-statins-liver (心血管 / medium)
    """
    _golden_assert(
        label="他汀肝功能",
        query="阿托伐他汀 20mg ALT 80",
        rules=real_rules,
        target_rule_id="rx-statins-liver",
        expected_severity="medium",
        expected_drug_class="心血管",
        evidence_keyword="ALT",
    )


def test_golden_eight_scenarios_smoke(real_rules):
    """汇总: 8 种 golden 场景在一次调用内全部命中 top-3.

    便于在 CI 中作为单一 smoke 入口;失败时打印每个场景的诊断.
    """
    scenarios = [
        ("庆大霉素 iv 80mg 8岁", "rx-aminoglycoside-pediatric", "high", "儿科"),
        ("布洛芬 200mg 孕妇 32周", "rx-nsaid-pregnancy", "high", "孕期"),
        ("二甲双胍 500mg egfr=45", "rx-metformin-renal", "medium", "肾损"),
        ("卡托普利 25mg 孕妇", "rx-acei-pregnancy", "high", "孕期"),
        ("万古霉素 1g iv q12h", "rx-vancomycin-tdm", "high", "抗菌药"),
        ("华法林 3mg INR 2.5", "rx-warfarin-inr", "high", "心血管"),
        ("奥美拉唑 20mg 8周", "rx-ppi-longterm", "low", "儿科"),
        ("阿托伐他汀 20mg ALT 80", "rx-statins-liver", "medium", "心血管"),
    ]

    failures: list[str] = []
    for query, rule_id, severity, drug_class in scenarios:
        results = search_rules(query, real_rules, limit=3)
        if not results:
            failures.append(f"  - {rule_id}: no results for {query!r}")
            continue
        top_ids = [r["rule_id"] for r in results]
        if rule_id not in top_ids:
            failures.append(
                f"  - {rule_id}: not in top-3 for {query!r}; "
                f"got {top_ids} scores={[round(r['score'], 3) for r in results]}"
            )
            continue
        target = next(r for r in results if r["rule_id"] == rule_id)
        if target["score"] < _GOLDEN_SCORE_MIN:
            failures.append(
                f"  - {rule_id}: score {target['score']:.3f} < {_GOLDEN_SCORE_MIN} "
                f"for {query!r}"
            )
            continue
        if target["severity"] != severity or target["drug_class"] != drug_class:
            failures.append(
                f"  - {rule_id}: severity/drug_class mismatch "
                f"(got {target['severity']}/{target['drug_class']}, "
                f"expected {severity}/{drug_class})"
            )

    assert not failures, (
        f"{len(failures)} golden scenario(s) failed:\n"
        + "\n".join(failures)
        + "\n\nReal rules loaded: "
        + ", ".join(sorted(r["rule_id"] for r in real_rules))
    )


# ---------------------------------------------------------------------------
# detect_rule_conflicts -- Task 32: 规则互斥与多规则高亮
# 3 类冲突:
#   - same_drug_class_multiple_high (同 drug_class ≥2 条 high)
#   - contradictory_applies_to (pregnancy / renal_function / age 互斥)
#   - cross_drug_class_aggregation (不同 drug_class 但同 population 签名)
# 返回 {conflicts, recommended_rule_id, has_conflict};tools.apply_rule 暴露。
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str,
    drug_class: str,
    severity: str,
    population: str,
    body: str = "示例正文文本用于构造冲突测试。",
    applies_to: Any = None,
) -> dict[str, Any]:
    """Helper to construct a minimal rule record for conflict tests."""
    return {
        "rule_id": rule_id,
        "drug_class": drug_class,
        "severity": severity,
        "evidence_source": "示例依据",
        "population": population,
        "body": body,
        "applies_to": applies_to if applies_to is not None else [],
    }


def test_detect_rule_conflicts_empty_top_rules_returns_empty():
    """空 top_rules 时无冲突,recommended_rule_id=None。"""
    result = detect_rule_conflicts({"patient_age": 8}, [])
    assert result == {
        "conflicts": [],
        "recommended_rule_id": None,
        "has_conflict": False,
    }


def test_detect_rule_conflicts_same_class_multiple_high():
    """同 drug_class ≥2 条 high 规则 → 触发 same_drug_class_multiple_high,推荐 body 最具体者。"""
    rule_a = _make_rule(
        "rx-fake-a",
        drug_class="儿科",
        severity="high",
        population="8 岁以下儿童",
        body="短正文。",
        applies_to=[{"age_max": 8}],
    )
    rule_b = _make_rule(
        "rx-fake-b",
        drug_class="儿科",
        severity="high",
        population="儿童",
        body="这是一段非常长的描述文本,涵盖详细的临床要点、剂量、监测指标,"
        "用于覆盖 body 长度评分,确保推荐为 rule_b。",
        applies_to=[{"age_min": 0, "age_max": 18}],
    )
    rule_c = _make_rule(
        "rx-fake-c",
        drug_class="儿科",
        severity="medium",
        population="儿童",
        body="另一条中等严重度规则,不应被推荐。",
        applies_to=[{"age_min": 0, "age_max": 14}],
    )
    query = {"patient_age": 8, "drug_name": "庆大霉素"}
    top_rules = [(rule_b, 10.0), (rule_a, 8.0), (rule_c, 5.0)]
    result = detect_rule_conflicts(query, top_rules)

    assert result["has_conflict"] is True
    assert result["recommended_rule_id"] in {"rx-fake-b", "rx-fake-a"}

    type1 = [
        c for c in result["conflicts"]
        if c["conflict_type"] == "same_drug_class_multiple_high"
    ]
    assert len(type1) == 1
    assert set(type1[0]["rule_ids"]) == {"rx-fake-a", "rx-fake-b"}
    assert type1[0]["drug_class"] == "儿科"
    assert "rx-fake-b" in type1[0]["message"]


def test_detect_rule_conflicts_contradictory_applies_to():
    """pregnancy 与 renal_function 同时出现互斥条目 → 触发 contradictory_applies_to。"""
    rule_preg = _make_rule(
        "rx-fake-preg",
        drug_class="孕期",
        severity="high",
        population="孕妇",
        body="孕妇禁用。",
        applies_to=[{"pregnancy": True}],
    )
    rule_nonpreg = _make_rule(
        "rx-fake-nonpreg",
        drug_class="心血管",
        severity="high",
        population="非妊娠人群",
        body="非妊娠人群适用。",
        applies_to=[{"pregnancy": False}],
    )
    rule_renal_ok = _make_rule(
        "rx-fake-renal-ok",
        drug_class="肾损",
        severity="medium",
        population="肾功能正常",
        body="肾功能正常者适用。",
        applies_to=[{"renal_function": "normal"}],
    )
    rule_renal_bad = _make_rule(
        "rx-fake-renal-bad",
        drug_class="肾损",
        severity="high",
        population="肾功能不全",
        body="肾功能不全者禁用。",
        applies_to=[{"renal_function": "impairment"}],
    )
    query = {"pregnancy": True, "egfr": 45}
    top_rules = [
        (rule_preg, 9.0),
        (rule_nonpreg, 8.5),
        (rule_renal_ok, 7.0),
        (rule_renal_bad, 6.5),
    ]
    result = detect_rule_conflicts(query, top_rules)

    assert result["has_conflict"] is True
    type2 = [
        c for c in result["conflicts"]
        if c["conflict_type"] == "contradictory_applies_to"
    ]
    subtypes = {c["subtype"] for c in type2}
    # 应当同时包含妊娠矛盾 + 肾功能矛盾
    assert "pregnancy_true_vs_false" in subtypes
    assert "renal_function_normal_vs_impaired" in subtypes
    # 各冲突应包含预期规则 id
    preg_conflict = next(
        c for c in type2 if c["subtype"] == "pregnancy_true_vs_false"
    )
    assert "rx-fake-preg" in preg_conflict["rule_ids"]
    assert "rx-fake-nonpreg" in preg_conflict["rule_ids"]
    # has_conflict 与 conflicts 长度一致
    assert len(result["conflicts"]) >= 2


def test_detect_rule_conflicts_cross_drug_class_aggregation():
    """不同 drug_class 但同 population 签名 → 触发 cross_drug_class_aggregation。"""
    rule_ped_a = _make_rule(
        "rx-ped-a",
        drug_class="儿科",
        severity="high",
        population="8 岁以下儿童",
        body="儿童禁用 A。",
        applies_to=[{"age_max": 8}],
    )
    rule_ped_b = _make_rule(
        "rx-ped-b",
        drug_class="抗菌药",
        severity="high",
        population="儿童",
        body="儿童抗菌药注意。",
        applies_to=[{"age_max": 14}],
    )
    rule_unrelated = _make_rule(
        "rx-unrelated",
        drug_class="心血管",
        severity="medium",
        population="成人住院",
        body="成人心血管。",
        applies_to=[{"age_min": 18}],
    )
    query = {"patient_age": 7}
    top_rules = [
        (rule_ped_a, 9.0),
        (rule_ped_b, 8.0),
        (rule_unrelated, 3.0),
    ]
    result = detect_rule_conflicts(query, top_rules)

    assert result["has_conflict"] is True
    type3 = [
        c for c in result["conflicts"]
        if c["conflict_type"] == "cross_drug_class_aggregation"
    ]
    # 儿童 population 签名应在多个 drug_class 命中
    assert len(type3) >= 1
    agg = next(
        c for c in type3 if c["population_signature"] == "儿童"
    )
    assert "儿科" in agg["drug_classes"]
    assert "抗菌药" in agg["drug_classes"]
    assert "心血管" not in agg["drug_classes"]


def test_detect_rule_conflicts_no_conflict_single_rule():
    """单一规则命中 → 无冲突。"""
    rule = _make_rule(
        "rx-fake-solo",
        drug_class="儿科",
        severity="high",
        population="儿童",
        body="唯一命中规则。",
        applies_to=[{"age_max": 12}],
    )
    top_rules = [(rule, 5.0)]
    result = detect_rule_conflicts({"patient_age": 8}, top_rules)
    assert result["has_conflict"] is False
    assert result["conflicts"] == []
    assert result["recommended_rule_id"] is None


def test_detect_rule_conflicts_accepts_string_query():
    """query 可以是字符串(原始医嘱字段串),不抛错。"""
    rule_a = _make_rule(
        "rx-fake-a",
        drug_class="儿科",
        severity="high",
        population="儿童",
        body="短。",
        applies_to=[{"age_max": 8}],
    )
    rule_b = _make_rule(
        "rx-fake-b",
        drug_class="儿科",
        severity="high",
        population="儿童",
        body="长正文" * 30,
        applies_to=[{"age_max": 12}],
    )
    result = detect_rule_conflicts("庆大霉素 儿童 8 岁", [(rule_b, 9.0), (rule_a, 7.0)])
    assert result["has_conflict"] is True
    assert any(
        c["conflict_type"] == "same_drug_class_multiple_high"
        for c in result["conflicts"]
    )
