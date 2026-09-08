"""Tests for pass_field_check.runtime._tokens_zh.

Task 6 spec: 中文分词扩展，覆盖英文 / 连续 CJK / 混合输入，丢弃单字噪声。
参考：github_ref/agency-agents/scripts/build-hermes-plugin.py L122-186。

Task 21 spec: 中英文药品通用名 / 商品名兜底词表，query 写英文也能命中中文
规则 body（反之亦然）。
"""
from __future__ import annotations

from pass_field_check.runtime import (
    _DRUG_NAME_ALIASES,
    _expand_aliases,
    _fuzzy_normalize,
    _tokens_zh,
    search_rules,
)


def test_tokens_zh_english():
    """An all-English query keeps the latin tokens.

    Note: as of Task 21 the alias table adds the canonical Chinese alias
    when an English INN is recognised (e.g. ``gentamicin`` → ``庆大霉素``).
    That is the intended behaviour: cross-language recall of rule bodies
    written in the other language. Non-drug English terms still produce
    no Chinese noise.
    """
    tokens = _tokens_zh("gentamicin 80mg IV q8h")
    assert "gentamicin" in tokens
    assert "80mg" in tokens
    assert "iv" in tokens
    assert "q8h" in tokens
    # Recognised drug name now expands to its Chinese canonical alias
    # (Task 21: cross-language recall).
    assert "庆大霉素" in tokens
    # Pure numbers / units / non-drug Latin words do not get Chinese aliases.
    plain_tokens = _tokens_zh("HbA1c 7.5 egfr 45")
    assert not any("一" <= ch <= "鿿" for ch in " ".join(plain_tokens))


def test_tokens_zh_chinese():
    """A Chinese query surfaces continuous CJK runs of >=2 chars."""
    tokens = _tokens_zh("庆大霉素 儿童 8 岁")
    assert "庆大霉素" in tokens
    assert "儿童" in tokens
    # '8' / '岁' 单字符不会出现在 CJK 段里 (因为 _ZH_RE 要求 >=2)。
    assert "8" not in tokens
    assert "岁" not in tokens


def test_tokens_zh_mixed():
    """Mixed English/Chinese query yields both Latin and CJK tokens."""
    tokens = _tokens_zh("阿莫西林 amoxicillin 500mg 口服")
    assert "阿莫西林" in tokens
    assert "amoxicillin" in tokens
    assert "500mg" in tokens
    assert "口服" in tokens
    # 'mg' 是 2 字符拉丁词，被保留 (>=2 才留)。
    # 'mg' 来自 '500mg' 的 findall 子串吗？regex 是非贪婪的连续匹配，
    # '500mg' 是一个 token → 已包含 mg 子串；显式验证：
    assert "500mg" in tokens


def test_tokens_zh_noise_filtered():
    """Single-char Chinese particles (的/了/是) are dropped.

    Note: ``_ZH_RE`` matches runs of >=2 CJK chars with no internal split,
    so the tokens come out as whole runs separated by whitespace / punctuation
    (the regex char class for CJK does not carve on internal boundaries --
    that mirrors how the source product's latin regex does not split on
    case changes either).
    """
    tokens = _tokens_zh("这里是阿莫西林的适应症，说明")
    assert "这里是阿莫西林的适应症" in tokens
    assert "说明" in tokens
    # 单字符虚词 '的' / '是' 不应独立成 token (前者属于上面长 run，后者被丢弃)。
    assert "的" not in tokens
    assert "是" not in tokens


def test_tokens_zh_empty_and_none_safe():
    """Empty / None input yields an empty set without raising."""
    assert _tokens_zh("") == set()
    # The function is not required to handle None, but passing empty string is
    # the realistic minimum (audit log fields may be missing).
    tokens = _tokens_zh("   \n\t  ")
    # Whitespace-only text contains no latin/cjk runs → empty set.
    assert tokens == set()


def test_tokens_zh_case_insensitive_latin():
    """Latin tokens are normalised to lowercase."""
    tokens = _tokens_zh("Gentamicin AMOXICILLIN")
    assert "gentamicin" in tokens
    assert "amoxicillin" in tokens
    assert "Gentamicin" not in tokens
    assert "AMOXICILLIN" not in tokens


def test_tokens_zh_special_chars_kept_as_token():
    """Allowed special chars (+ . # _) are kept inside a token, not split.

    Note: the regex character class is ``[a-z0-9+.#_-]`` -- ``%`` is not in
    the set (matching build-hermes-plugin.py L122 source behaviour). Tokens
    split at ``%`` so ``7.5%`` becomes ``7.5`` plus the dropped ``%``.
    ``+`` *is* in the class, so ``Na+`` stays as one token.
    """
    tokens = _tokens_zh("HbA1c 7.5% egfr=45 ACE-I Na+")
    assert "hba1c" in tokens
    # The dot is kept inside the token; ``%`` is not in the char class, so
    # it ends the run at ``7.5``.
    assert "7.5" in tokens
    assert "7.5%" not in tokens
    assert "egfr" in tokens
    assert "45" in tokens
    assert "ace-i" in tokens
    assert "na+" in tokens
    # ``=`` is not in the class either; both sides stand alone.
    assert "=" not in tokens


def test_tokens_zh_single_latin_letter_dropped():
    """Standalone single letters / digits (a, i, 1) are dropped."""
    tokens = _tokens_zh("a I 1 阿莫西林")
    assert "阿莫西林" in tokens
    assert "a" not in tokens
    assert "i" not in tokens
    assert "1" not in tokens


# ---------------------------------------------------------------------------
# Task 21: 中英文药品通用名 / 商品名兜底词表
# ---------------------------------------------------------------------------


def test_drug_aliases_zh_to_en():
    """A Chinese drug name in the query expands to its English aliases."""
    tokens = _tokens_zh("庆大霉素 儿童 80mg iv")
    # Original Chinese token preserved
    assert "庆大霉素" in tokens
    # Alias expansion adds the English INN as well, so downstream
    # field-level overlap can match rules whose body uses gentamicin.
    assert "gentamicin" in tokens
    assert "gentamycin" in tokens


def test_drug_aliases_en_to_zh():
    """An English drug name in the query expands to its Chinese alias."""
    tokens = _tokens_zh("vancomycin 1g iv q12h TDM")
    # Original English token preserved
    assert "vancomycin" in tokens
    # Reverse lookup adds the canonical Chinese key so rules with
    # body containing '万古霉素' can be matched.
    assert "万古霉素" in tokens


def test_drug_aliases_bidirectional_query():
    """A query mixing English and Chinese drug names expands to both sides."""
    tokens = _tokens_zh("ciprofloxacin 儿童 6 岁")
    assert "ciprofloxacin" in tokens
    assert "cipro" in tokens
    assert "环丙沙星" in tokens
    # Children context preserved
    assert "儿童" in tokens


def test_drug_aliases_no_alias_unchanged():
    """An ordinary query with no drug names is unaffected by the alias table."""
    tokens = _tokens_zh("今天 天气 不错")
    # No alias map entries are common Chinese function words.
    assert "今天" in tokens
    assert "天气" in tokens
    assert "不错" in tokens
    # The alias table should not pollute the result with unexpected English.
    assert "gentamicin" not in tokens
    assert "vancomycin" not in tokens


def test_drug_aliases_alias_dictionary_size():
    """The built-in alias table covers at least 10 drug name pairs."""
    assert len(_DRUG_NAME_ALIASES) >= 10
    # Spot-check canonical entries the rest of the test suite depends on.
    assert "庆大霉素" in _DRUG_NAME_ALIASES
    assert "万古霉素" in _DRUG_NAME_ALIASES
    assert "环丙沙星" in _DRUG_NAME_ALIASES
    assert "布洛芬" in _DRUG_NAME_ALIASES


def test_drug_aliases_search_recall_chinese_to_english():
    """English query 庆大霉素 儿童 hits rx-aminoglycoside-pediatric in top-3.

    This exercises the full pipeline: tokeniser alias expansion feeds into
    _score_rx, which then computes field-level overlap on the rule body
    (whose drug_class is written as '儿科' and whose body uses both
    庆大霉素 and gentamicin interchangeably).
    """
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("庆大霉素 儿童 8 岁", rules, limit=5)
    assert results, "expected at least one rule hit"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-aminoglycoside-pediatric" in top_ids


def test_drug_aliases_search_recall_english_to_chinese_body():
    """English query 'vancomycin 1g iv TDM' hits rx-vancomycin-tdm via alias.

    Without the alias table, the body of rx-vancomycin-tdm contains
    '万古霉素' but the query only contains 'vancomycin', so the token
    overlap on the body field would be 0 and the rule would not surface.
    """
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("vancomycin 1g iv TDM", rules, limit=5)
    assert results, "expected at least one rule hit for English vancomycin"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-vancomycin-tdm" in top_ids


def test_expand_aliases_helper_idempotent():
    """The helper preserves originals and does not re-process expanded tokens.

    Calling _expand_aliases on a set that already contains an alias value
    should still return that value (no double-mapping, no dropped tokens).
    """
    base = {"gentamicin", "儿童"}
    out = _expand_aliases(base, _DRUG_NAME_ALIASES)
    assert "gentamicin" in out
    assert "儿童" in out
    assert "庆大霉素" in out


def test_expand_aliases_empty_inputs():
    """Empty input set / empty alias table returns the original set."""
    assert _expand_aliases(set(), _DRUG_NAME_ALIASES) == set()
    assert _expand_aliases({"gentamicin"}, {}) == {"gentamicin"}
    assert _expand_aliases(set(), {}) == set()


# ---------------------------------------------------------------------------
# Task 30: 药品名拼写容错（编辑距离 fuzzy match）
# ---------------------------------------------------------------------------


def test_fuzzy_normalize_exact_match_returns_canonical_key():
    """An exact canonical-key token returns that key verbatim."""
    out = _fuzzy_normalize("庆大霉素", _DRUG_NAME_ALIASES)
    assert out == ["庆大霉素"]


def test_fuzzy_normalize_exact_value_match_returns_canonical_key():
    """An exact alias-value token returns the canonical Chinese key."""
    out = _fuzzy_normalize("gentamicin", _DRUG_NAME_ALIASES)
    assert "庆大霉素" in out
    assert "gentamicin" in out


def test_fuzzy_normalize_typo_one_char_chinese():
    """One-character Chinese typo (庆大梅素) maps back to 庆大霉素.

    Distance is 1 (substitution 梅↔霉), within the default
    ``_MAX_EDIT_DISTANCE``. ``青霉素`` would be distance 2 to ``庆大霉素``
    and is therefore *not* matched by default.
    """
    out = _fuzzy_normalize("庆大梅素", _DRUG_NAME_ALIASES)
    assert "庆大霉素" in out


def test_fuzzy_normalize_typo_two_chars_not_matched_by_default():
    """A two-character Chinese typo (庆大梅速) is too far at distance 1.

    With the default ``max_edit_distance=1`` the function returns an
    empty list -- preventing accidental collisions like '青霉素' →
    '庆大霉素' (distance 2).
    """
    out = _fuzzy_normalize("庆大梅速", _DRUG_NAME_ALIASES)
    assert out == []


def test_fuzzy_normalize_typo_two_chars_matched_when_distance_2_allowed():
    """The same typo with max_edit_distance=2 *does* fire.

    Tests the override knob so callers can opt into looser matching
    when they want it (e.g. OCR-based prescription digitisation).
    """
    out = _fuzzy_normalize(
        "庆大梅速", _DRUG_NAME_ALIASES, max_edit_distance=2
    )
    assert "庆大霉素" in out


def test_fuzzy_normalize_unrelated_drug_not_matched():
    """An unrelated drug name (青霉素) does NOT map to 庆大霉素.

    Distance between 青霉素 and 庆大霉素 is 2 (substitute 青↔庆, 霉↔霉,
    素↔素) -- one substitution. Wait: 青霉 vs 庆大霉 differs by 2
    characters, 素 == 素. Total edit distance is 2. With default
    max_edit_distance=1 the typo pass is silent.
    """
    out = _fuzzy_normalize("青霉素", _DRUG_NAME_ALIASES)
    assert out == []


def test_fuzzy_normalize_english_typo():
    """An English one-character typo (aminoglycosid) maps to aminoglycoside.

    Verifies the alias value side also benefits from fuzzy matching.
    Note: aminoglycoside is *not* in our alias table by name (the table
    covers INNs like gentamicin / amoxicillin), but the canonical key
    '庆大霉素' is a member of the aminoglycoside class.  We test a
    different in-table typo instead: 'gentamicn' (missing the trailing
    'i') → distance 1 → gentamicin → 庆大霉素.
    """
    out = _fuzzy_normalize("gentamicn", _DRUG_NAME_ALIASES)
    assert "庆大霉素" in out
    assert "gentamicin" in out


def test_fuzzy_normalize_unknown_token_returns_empty():
    """An unrelated token returns [] without raising."""
    assert _fuzzy_normalize("xyzpdq", _DRUG_NAME_ALIASES) == []
    # CJK garbage also returns []
    assert _fuzzy_normalize("随便写写", _DRUG_NAME_ALIASES) == []


def test_fuzzy_normalize_empty_inputs_safe():
    """Empty token / empty dict returns [] without raising."""
    assert _fuzzy_normalize("", _DRUG_NAME_ALIASES) == []
    assert _fuzzy_normalize("gentamicin", {}) == []
    assert _fuzzy_normalize("", {}) == []


def test_fuzzy_normalize_deduplicates_results():
    """When the same canonical key is hit twice (key-side + value-side)
    the result list contains the canonical key only once.

    Uses ``"metformn"`` (drop one ``i`` from ``metformin``; edit
    distance = 1) so the fuzzy pass fires on the value side and
    emits the canonical key once.  We then assert the canonical key
    appears at most once in the output list.
    """
    out = _fuzzy_normalize("metformn", _DRUG_NAME_ALIASES)
    # Both sides of the alias (canonical + value) are emitted once.
    assert "二甲双胍" in out
    assert "metformin" in out
    # And critically: de-duplicated.
    assert out.count("二甲双胍") == 1
    assert out.count("metformin") == 1


def test_tokens_zh_fuzzy_typo_recall_chinese():
    """A misspelt Chinese query '庆大梅素 儿童' surfaces 庆大霉素 via search.

    Fuzzy recall is applied in :func:`search_rules` (once per query)
    rather than inside :func:`_tokens_zh` (which is also invoked on
    every rule field during scoring, where the fuzzy pass would be
    wasted work).  This test verifies the end-to-end path: query →
    tokenise → fuzzy expand → field-level overlap → top-3 hit.
    """
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("庆大梅素 儿童", rules, limit=5)
    assert results, "expected at least one rule hit for fuzzy Chinese typo"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-aminoglycoside-pediatric" in top_ids


def test_tokens_zh_fuzzy_typo_recall_english():
    """A misspelt English query 'metformn' surfaces metformin/二甲双胍 via search.

    ``metformn`` is edit-distance 1 from ``metformin`` (one deletion);
    the standard swap typo ``metfromin`` would be distance 2 and is
    intentionally rejected by the default ``max_edit_distance=1`` pass.
    This test verifies the accepted 1-char typo path through the full
    pipeline rather than asserting on raw ``_tokens_zh`` output.
    """
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("metformn 500mg", rules, limit=5)
    assert results, "expected at least one rule hit for fuzzy English typo"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-metformin-renal" in top_ids


def test_search_fuzzy_typo_chinese_query_top3():
    """Fuzzy Chinese typo '庆大梅素 儿童 8 岁' hits rx-aminoglycoside-pediatric.

    Exercises the full pipeline: tokeniser → fuzzy expand → field-level
    overlap → severity rank → top-3 trim.  The misspelt query must
    still surface the aminoglycoside pediatric rule via the fuzzy pass.
    """
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("庆大梅素 儿童 8 岁", rules, limit=5)
    assert results, "expected at least one rule hit for fuzzy Chinese typo"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-aminoglycoside-pediatric" in top_ids


def test_search_fuzzy_typo_english_query_top3():
    """Fuzzy English typo 'metformn egfr 45' hits rx-metformin-renal."""
    from pathlib import Path

    from pass_field_check.index_builder import collect_rx_rules

    rules_dir = Path(__file__).resolve().parent.parent / "rules"
    rules = collect_rx_rules(rules_dir)

    results = search_rules("metformn egfr 45", rules, limit=5)
    assert results, "expected at least one rule hit for fuzzy English typo"
    top_ids = [r["rule_id"] for r in results[:3]]
    assert "rx-metformin-renal" in top_ids


def test_edit_distance_basic_values():
    """Spot-check the underlying edit-distance function."""
    # Module-private symbol intentionally imported here for the spot-check.
    from pass_field_check.runtime import _edit_distance

    assert _edit_distance("", "") == 0
    assert _edit_distance("abc", "abc") == 0
    assert _edit_distance("", "abc") == 3
    assert _edit_distance("abc", "") == 3
    assert _edit_distance("kitten", "sitting") == 3
    # The case study from the docstring: 庆大霉素 → 庆大梅素 = 1.
    assert _edit_distance("庆大霉素", "庆大梅素") == 1
    # gentamicin → gentamicn = 1.
    assert _edit_distance("gentamicin", "gentamicn") == 1
    # 青霉素 → 庆大霉素 = 2.
    assert _edit_distance("青霉素", "庆大霉素") == 2
