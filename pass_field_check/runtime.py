"""运行时核心：分词、打分、检索、审方意见拼装。

源码产品能力参考：github_ref/agency-agents/scripts/build-hermes-plugin.py
  - _tokens (L132-133) → 复用其小写归一 + regex findall 的最小集合思路。
  - _score (L162-186) → 复用其多字段加权 + 子串命中加分 + tie-break
    偏好短文的思想 + severity 二级排序。

本模块在源产品能力上扩展中文覆盖：审方场景下 query 与规则 body 都大量
包含中文药品通用名 / 商品名 / 适应症 / 科室名，仅保留英文 token 会
导致跨字段串召回率几乎为零。
"""
from __future__ import annotations

import math
import re
import warnings
from typing import Any

# English / Latin token: drugs, fields, units, abbreviations. Mirrors
# build-hermes-plugin.py:122 _WORD_RE semantics.
_EN_RE = re.compile(r"[a-z0-9][a-z0-9+.#_-]*", re.I)

# Continuous CJK run (>=2 chars).  Single-char Chinese is mostly function
# particles (的 / 了 / 是 / 在) and would add noise if kept.
_ZH_RE = re.compile(r"[一-龥]{2,}")

# Tokens shorter than this length (in CJK chars) are too generic to be
# informative in a search index.  Mirrors how the source product's _tokens
# drops empty/short Latin noise.
_MIN_TOKEN_LEN = 2

# Maximum length of CJK substring emitted inside long CJK runs.  Most
# Chinese drug generic names are 3-4 characters; allowing up to 6 covers
# compound names like '阿莫西林克拉维酸' while keeping the token set
# bounded for very long body sentences.
_MAX_CJK_SUBSTR = 6

# ---------------------------------------------------------------------------
# 药品名兜底词表（中英文互译）
# ---------------------------------------------------------------------------
# 真实审方场景下，医师常以英文 INN 缩写 / 商品名在医嘱字段串中录入（如
# ``gentamicin 80mg iv q8h``），而规则 body 写中文通用名（``庆大霉素``）。
# 反之亦有（药师在工作站手填 ``万古霉素 TDM`` 而规则 body 写
# ``vancomycin``）。仅靠 _tokens_zh 字符级切分会让跨字段串召回率几乎
# 为零；通过内置常见中英文药品名别名映射预处理，可让任一边出现即可命中。
#
# 设计取舍：
#   - 词表内置常量即可；不引入外部词典（医院自建端点 / CI 友好）；
#   - 双向别名（zh↔en），键既可能是 INN 通用名也可能是常用商品名；
#   - 词表 ≥10 对；扩展由药事办 / 院感 / 信息科在版本化迭代里追加；
#   - 兼顾 _MAX_CJK_SUBSTR=6 的 CJK 子串扩展（阿莫西林 / 万古霉素 等
#     会被独立成 token），别名表仅在跨中英文查询方向补充召回。
_DRUG_NAME_ALIASES: dict[str, list[str]] = {
    # key 中文通用名 → value [英文 INN, 常见别名]
    "庆大霉素": ["gentamicin", "gentamycin"],
    "阿莫西林": ["amoxicillin", "amoxil"],
    "二甲双胍": ["metformin", "glucophage"],
    "头孢曲松": ["ceftriaxone", "rocephin"],
    "万古霉素": ["vancomycin", "vancocin"],
    "环丙沙星": ["ciprofloxacin", "cipro"],
    "卡托普利": ["captopril", "capoten"],
    "依那普利": ["enalapril", "vasotec"],
    "阿昔洛韦": ["acyclovir", "aciclovir", "zovirax"],
    "布洛芬": ["ibuprofen", "motrin", "advil"],
    "奥美拉唑": ["omeprazole", "prilosec", "losec"],
    "华法林": ["warfarin", "coumadin"],
    "甲硝唑": ["metronidazole", "flagyl"],
    "地高辛": ["digoxin", "lanoxin"],
    "阿托伐他汀": ["atorvastatin", "lipitor"],
}

# Severity ordering: high first, then medium, then low. Used as a secondary
# sort key over the raw token-overlap score so that, when two rules hit the
# same score, the more clinically dangerous rule surfaces first.
#
# Public so callers / tests can reason about ordering without inspecting
# private internals. Mirrors the source product's severity-rank convention
# (build-hermes-plugin.py L168-180 keeps the same high/medium/low ladder).
SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# Field weights for token-overlap scoring. The source product (_score at
# L162-186) gives a flat overlap count plus name/description bonuses. Here
# the same idea is adapted to the rule schema: rule_id (slug) is the most
# authoritative, drug_class and population carry clinical intent, evidence
# source is metadata, and body is the long tail.
_FIELD_WEIGHTS: dict[str, float] = {
    "rule_id": 3.0,
    "drug_class": 2.0,
    "population": 1.5,
    "evidence_source": 1.0,
    "body": 0.5,
}


def _expand_aliases(tokens: set[str], aliases: dict[str, list[str]]) -> set[str]:
    """Expand ``tokens`` with bidirectional drug-name aliases.

    For each token in ``tokens`` that matches an alias key (either a Chinese
    generic name or an English INN) the corresponding alias list is merged
    into the result.  Matching is case-insensitive on the Latin side.

    The function is intentionally additive: the original token is preserved
    so consumers that rely on the exact token still see it.  This avoids
    silently replacing ``"gentamicin"`` with ``"庆大霉素"`` and breaking
    tests / summaries that expected the original spelling.
    """
    if not tokens or not aliases:
        return set(tokens)
    lowered_aliases: dict[str, list[str]] = {
        key.lower(): [v.lower() for v in vals] for key, vals in aliases.items()
    }
    out: set[str] = set(tokens)
    for token in tokens:
        key = token.lower()
        if key in lowered_aliases:
            out.update(lowered_aliases[key])
        else:
            # Reverse lookup: token may be an alias value (e.g. "gentamicin"
            # written by the user) — add the canonical Chinese key so the
            # downstream field-level token-overlap can match Chinese rules.
            for canonical, alts in aliases.items():
                if any(token.lower() == a.lower() for a in alts):
                    out.add(canonical)
                    out.update(a.lower() for a in alts)
                    break
    return out


def _tokens_zh(text: str) -> set[str]:
    """Return normalised token set covering both Latin and continuous CJK runs.

    参考：build-hermes-plugin.py L132 _tokens 思路 (regex findall + lowercase +
    去重为 set)。本实现扩展为:
      - 英文 / 拉丁: ``[a-z0-9][a-z0-9+.#_-]*`` → 药名 / 字段 / 缩写 / 单位
      - 中文: ``[一-龥]{2,}`` → 药品通用名 / 商品名 / 适应症 / 科室名
      - 长 CJK 段内的 2..6 字子串 → 让 ``如庆大霉素`` 这种被虚词前缀
        吞掉的药名仍能以 ``庆大霉素`` 形式出现在 token 集合中
      - 中英文药名兜底别名 (_DRUG_NAME_ALIASES) → 让 ``gentamicin`` query
        也能命中含 ``庆大霉素`` 的规则 body,反之亦然
      - 过滤单字符 CJK 与空串 (避免 '的' / '了' 等虚词噪声)
      - 全部小写归一
    """
    if not text:
        return set()

    en_tokens = {token.lower() for token in _EN_RE.findall(text)}
    # re.findall without capture groups returns strings directly.
    zh_tokens = set(_ZH_RE.findall(text))

    # Emit length-2..6 substrings for any CJK run long enough to swallow a
    # drug name.  This is bounded (a run of length N contributes at most
    # (N-1) + (N-2) + ... + max(0, N-5) tokens, capped by _MAX_CJK_SUBSTR).
    substr_tokens: set[str] = set()
    for run in zh_tokens:
        n = len(run)
        if n < 3:
            # A 2-char run has no useful substring other than itself.
            continue
        upper = min(n, _MAX_CJK_SUBSTR)
        for length in range(2, upper + 1):
            for start in range(n - length + 1):
                substr_tokens.add(run[start : start + length])

    # Drop single-character noise (the regex already enforces >=2 for CJK,
    # but Latin single chars such as 'a' / 'i' are also useless as tokens).
    base_tokens: set[str] = set()
    for token in en_tokens | zh_tokens | substr_tokens:
        if not token:
            continue
        if len(token) < _MIN_TOKEN_LEN:
            continue
        base_tokens.add(token)

    # Bidirectional drug-name alias expansion so cross-language queries still
    # recall rules whose body is written in the other language.
    return _expand_aliases(base_tokens, _DRUG_NAME_ALIASES)


def _score_rx(query_tokens: set[str], rule: dict[str, Any]) -> float:
    """Token-overlap score for one rule against the user query.

    Mirrors build-hermes-plugin.py:162-186 ``_score``:
      - count overlapping tokens between query_tokens and the rule's haystack;
      - add per-field bonuses when a query token appears in a stronger field
        (rule_id / drug_class / population / evidence_source / body);
      - return 0.0 if there is no overlap at all (so search_rules can drop it
        cheaply without polluting the top-N with junk).

    Hospital-specific extension: the original product treats ``name`` /
    ``description`` as strong signals. Here the same role is played by
    ``rule_id`` (the slug is the authoritative identifier — touching a query
    token that hits the slug is near-certain relevance) and ``drug_class``
    (the user almost always knows which drug class they are scanning).
    ``body`` carries the long tail of clinical detail.

    The returned value is a raw weighted overlap sum (not normalised); the
    caller (``search_rules``) ranks by this score and applies severity
    weighting downstream.
    """
    if not query_tokens:
        return 0.0

    score = 0.0

    # rule_id gets a flat bonus if any query token literally equals (or is
    # contained in) the slug.  We use substring containment rather than full
    # equality because query tokens may be partial (e.g. 'aminoglycoside' is
    # a substring of 'rx-aminoglycoside-pediatric').
    rule_id_text = str(rule.get("rule_id", "")).lower()
    if rule_id_text:
        for token in query_tokens:
            if token and (token in rule_id_text):
                score += _FIELD_WEIGHTS["rule_id"]
                break  # only count once per rule

    # Per-field overlap.  For each field we compute the per-field token set
    # and count overlapping tokens, then multiply by the field weight.
    for field, weight in _FIELD_WEIGHTS.items():
        if field == "rule_id":
            continue  # handled above
        raw = rule.get(field, "")
        if not raw:
            continue
        field_tokens = _tokens_zh(str(raw))
        overlap = query_tokens & field_tokens
        if overlap:
            score += weight * len(overlap)

    return score


def _summary_rx(rule: dict[str, Any], score: float) -> dict[str, Any]:
    """Trim a rule dict to the public search-result shape.

    参考 build-hermes-plugin.py:189-200 ``_summary`` -- the source product
    trims the agent record down to slug / name / division / description /
    vibe / source_path plus the score.  Here we keep the same idea but with
    the hospital rule schema: rule_id (==slug), drug_class, severity,
    evidence_source, population, and file_path for traceability.
    """
    return {
        "rule_id": rule.get("rule_id", ""),
        "drug_class": rule.get("drug_class", ""),
        "severity": rule.get("severity", ""),
        "evidence_source": rule.get("evidence_source", ""),
        "population": rule.get("population", ""),
        "file_path": rule.get("file_path", ""),
        "score": round(float(score), 4),
    }


# ---------------------------------------------------------------------------
# 药品名拼写容错（编辑距离 fuzzy match）
# ---------------------------------------------------------------------------
# 真实审方场景下，医师 / 药师在手填医嘱字段串时常出现拼写错误
# （"庆大梅素" → "庆大霉素"、"aminoglycosid" → "aminoglycoside"、
# "metformn" → "metformin" 等）。仅靠精确 token-overlap 与别名表
# 会让这些临床真实错字被丢弃，结果误判为"无适用规则"，对
# 安全边界不利。本模块在不引入第三方 fuzzy 库的前提下，手写编辑距离
# （动态规划 O(mn)）对 query token 与 _DRUG_NAME_ALIASES 的 key /
# value 做 1 字符以内的归一化召回。
#
# 设计取舍：
#   - max_edit_distance 默认 1（与临床真实错字频度匹配：1 字符错字占
#     临床 80%+ 拼写错误；距离 2 会把 "青霉素" 错配到 "庆大霉素"，风险太高）；
#   - 不替换原 token，而是叠加别名侧（与 _expand_aliases 行为一致），
#     便于精确 token 与 fuzzy token 共存于检索集合；
#   - 大小写归一仅作用于比较侧，不修改 caller 的原 token；
#   - 双向：对 alias 表的 key 与 value 都参与编辑距离匹配，
#     query 写"庆大梅素"也能命中 canonical "庆大霉素"；
#   - **长度过滤**：drug 通用名 3–13 字符；超长 token（规则 body 内
#     切出的长 CJK 段、英文长复合词）直接放弃 fuzzy 召回，性能基线；
#   - **结果缓存**：同一 token 反复出现（合成 query + 100 次循环）
#     时 LRU 短路，避免重复 O(N) 编辑距离计算。
_MAX_EDIT_DISTANCE = 1

# Drug-name length window: edit distance ≥ 1 against a target of length
# N is impossible when ``|len(token) - N| > max_edit_distance``, so any
# token outside this window is structurally unmatchable.  Hard-coded
# from the alias table: longest key = 5 CJK chars ("阿托伐他汀"), longest
# value = 13 Latin chars ("ciprofloxacin").  The 1-char slack on each
# side covers one insertion / deletion.
_FUZZY_MIN_LEN = 2
_FUZZY_MAX_LEN = 14

# Bounded result cache so repeated synthetic / production queries hit a
# constant-time dict lookup instead of re-running edit distance.  Capped
# at 1024 entries; older entries are dropped FIFO (cheap ``dict`` insert
# ordering, no ``OrderedDict`` dependency).
_FUZZY_RESULT_CACHE: dict[tuple[str, int], list[str]] = {}
_FUZZY_CACHE_LIMIT = 1024


def _edit_distance(
    a: str,
    b: str,
    max_distance: int | None = None,
) -> int:
    """Return the Levenshtein edit distance between ``a`` and ``b``.

    Standard dynamic-programming implementation: ``dp[i][j]`` is the
    minimum edit cost to transform ``a[:i]`` into ``b[:j]``. Operations
    counted are insertion, deletion, and substitution (each cost 1).
    Pure Python ``list`` of ``int`` rows keeps the dependency surface
    zero (no NumPy / third-party libraries). O(len(a) * len(b)) time and
    O(min(len(a), len(b))) memory (rolling row trick).

    When ``max_distance`` is supplied, the function may short-circuit
    and return ``max_distance + 1`` as soon as the actual distance is
    guaranteed to exceed that bound.  Callers that only need a yes/no
    "within N edits?" answer should pass the bound and check the
    return value against it.  Without ``max_distance``, the function
    always returns the exact distance (used by tests / spot-checks).

    Mirrors the spirit of the source product's id-style dedup in
    build-hermes-plugin.py (slugification via ``[^a-z0-9]+`` → ``-``),
    but operates on characters rather than regex substitution; the source
    product does not implement edit-distance fuzzy, so this is a
    hospital-specific extension layered on top of the same alias
    dictionary.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # Trivial length-based lower bound: any edit must close this gap first.
    if max_distance is not None and abs(len(a) - len(b)) > max_distance:
        return max_distance + 1

    # Ensure the inner loop runs over the shorter string so memory stays
    # bounded (clinically this matters for Chinese generic names up to 6
    # chars vs short Latin abbreviations like 'INR').
    if len(a) < len(b):
        a, b = b, a

    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current_row = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            replace_cost = previous_row[j - 1] + (0 if ca == cb else 1)
            cell = min(insert_cost, delete_cost, replace_cost)
            if cell < row_min:
                row_min = cell
            current_row.append(cell)
        if max_distance is not None and row_min > max_distance:
            # No cell in this row can drop to <= max_distance in later
            # columns (all subsequent rows are monotonically non-
            # decreasing along their diagonal). Bail out.
            return max_distance + 1
        previous_row = current_row

    return previous_row[-1]


def _fuzzy_normalize(
    token: str,
    alias_dict: dict[str, list[str]],
    max_edit_distance: int = _MAX_EDIT_DISTANCE,
) -> list[str]:
    """Return a list of canonical alias keys that ``token`` is close to.

    Behaviour:
      - exact case-insensitive match against an alias key → return that key
        verbatim;
      - exact case-insensitive match against an alias value → return the
        corresponding canonical key (so ``"vancomycin"`` yields ``"万古霉素"``);
      - otherwise scan every key + value in ``alias_dict`` and collect any
        candidate whose edit distance to ``token`` is ``<= max_edit_distance``
        (default 1). The candidate is returned in the *canonical* form
        (i.e. the alias key when matching a value; the alias key when
        matching a key);
      - the returned list is de-duplicated, preserving insertion order;
      - an empty / unknown ``token`` returns ``[]`` (no false positives);
      - matching is case-insensitive on the Latin side and codepoint-
        exact on the CJK side;
      - tokens outside the ``[_FUZZY_MIN_LEN, _FUZZY_MAX_LEN]`` window
        are rejected structurally (drug-name lengths only) so the per-
        token cost stays bounded;
      - results are memoised in ``_FUZZY_RESULT_CACHE`` keyed by
        ``(lowered_token, max_edit_distance)`` so repeated tokens in
        synthetic / production query workloads hit a constant-time dict
        lookup.

    The function is read-only on ``alias_dict`` and does not mutate the
    caller's ``token``.  It is the inner helper called from
    :func:`_tokens_zh` after the precise-token expansion pass.
    """
    if not token or not alias_dict:
        return []

    lowered_token = token.lower()
    cache_key = (lowered_token, max_edit_distance)
    cached = _FUZZY_RESULT_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    if not (_FUZZY_MIN_LEN <= len(lowered_token) <= _FUZZY_MAX_LEN):
        _FUZZY_RESULT_CACHE[cache_key] = []
        return []

    seen: set[str] = set()
    out: list[str] = []

    def _emit(canonical: str) -> None:
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)

    # Pass 1: exact case-insensitive match against canonical keys.
    for key in alias_dict:
        if key.lower() == lowered_token:
            _emit(key)
            _memoize(cache_key, out)
            return out  # exact match is the strongest signal; no fuzzy needed.

    # Pass 2: exact case-insensitive match against alias values (reverse
    # lookup).  Emit the canonical key so downstream token-overlap on a
    # Chinese rule body still gets triggered.
    for key, values in alias_dict.items():
        for v in values:
            if v.lower() == lowered_token:
                _emit(key)
                _emit(v)
                _memoize(cache_key, out)
                return out

    # Pass 3: fuzzy match within max_edit_distance.  Iterate every
    # candidate on both sides (canonical key + alias value); emit the
    # canonical key for value-side matches so the search index receives
    # the same string the rules were written against.  Pass the budget
    # to ``_edit_distance`` so it short-circuits as soon as a row's
    # minimum exceeds the bound (constant-time amortisation on the
    # working set).
    for key, values in alias_dict.items():
        if _edit_distance(key.lower(), lowered_token, max_edit_distance) <= max_edit_distance:
            _emit(key)
        for v in values:
            if _edit_distance(v.lower(), lowered_token, max_edit_distance) <= max_edit_distance:
                _emit(key)
                _emit(v)

    _memoize(cache_key, out)
    return out


def _memoize(key: tuple[str, int], value: list[str]) -> None:
    """Insert ``value`` into ``_FUZZY_RESULT_CACHE`` with FIFO eviction.

    Kept module-private so tests can poke at the cache without breaking
    the public API.  The cap is intentionally generous (1024) -- the
    alias dictionary is closed-set, so the effective working set is
    bounded by ``num_unique_tokens * max_edit_distance_options`` which
    in practice stays well under 200 entries for clinical workloads.
    """
    if len(_FUZZY_RESULT_CACHE) >= _FUZZY_CACHE_LIMIT:
        # Drop the oldest ~10% to amortise eviction cost.
        for old_key in list(_FUZZY_RESULT_CACHE.keys())[: _FUZZY_CACHE_LIMIT // 10]:
            _FUZZY_RESULT_CACHE.pop(old_key, None)
    _FUZZY_RESULT_CACHE[key] = list(value)


# ---------------------------------------------------------------------------
# evidence_excerpt: 从规则 body 抽取 top 句相关片段
# 用于 audit 与审方意见 JSON 的 evidence_excerpt 字段，
# 让药师快速看到「这条规则的哪一句话支持了本次命中」。
# ---------------------------------------------------------------------------


# Sentence boundary characters used to split Chinese rule bodies into
# rough sentences.  English periods / question marks are deliberately not
# included: most rule bodies in this product are Chinese clinical text, and
# adding '.' would chop drug doses / abbreviations mid-value (e.g. '500mg q8h.').
_SENTENCE_DELIMS = "。；\n"


def _split_sentences_zh(body: str) -> list[str]:
    """Split a rule body into rough sentences on ``。/；/\\n``.

    Keeps only sentences with at least one non-whitespace character so the
    caller does not have to filter empty fragments.

    Rationale: markdown headers (e.g. ``### 临床要点``) survive because
    they sit on their own line; each is treated as its own sentence and
    can be ranked by token overlap independently.
    """
    if not body:
        return []
    pieces: list[str] = []
    buf: list[str] = []
    for ch in body:
        if ch in _SENTENCE_DELIMS:
            if buf:
                pieces.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        pieces.append("".join(buf))
    return [seg.strip() for seg in pieces if seg and seg.strip()]


def select_top_sentence(query_tokens: set[str], sentences: list[str]) -> str:
    """Return the sentence with the highest token-overlap with ``query_tokens``.

    Ties (multiple sentences sharing the same overlap count) are broken by
    sentence length ascending so the focused sentence wins over a longer
    paraphrase.  Returns an empty string when ``sentences`` is empty.

    Kept as a top-level helper (rather than nested inside
    ``extract_evidence_excerpt``) so tests can exercise the ranking logic
    without the truncation layer.
    """
    if not sentences:
        return ""
    best_sentence = ""
    best_overlap = -1
    for sentence in sentences:
        tokens = _tokens_zh(sentence)
        if not tokens:
            # A sentence that tokenises to nothing (e.g. all punctuation)
            # is ranked below anything with at least one token.
            overlap = -1
        else:
            overlap = len(tokens & query_tokens)
        if overlap > best_overlap or (
            overlap == best_overlap
            and overlap >= 0
            and len(sentence) < len(best_sentence)
        ):
            best_overlap = overlap
            best_sentence = sentence
    return best_sentence


def extract_evidence_excerpt(
    query_tokens: set[str],
    body: str,
    max_chars: int = 200,
) -> str:
    """Return the most-relevant sentence fragment from ``body`` for ``query_tokens``.

    Behaviour:
      - empty body / empty query_tokens → empty string (caller can decide
        whether to substitute a placeholder; we do not silently invent
        text for audit logs).
      - ``body`` is split into rough Chinese sentences via
        :func:`_split_sentences_zh`.
      - the sentence with the highest token-overlap with ``query_tokens``
        wins; ties break on shorter length (preferring focused detail).
      - the chosen sentence is returned as-is if its ``len()`` <=
        ``max_chars``; otherwise it is truncated to ``max_chars`` Python
        characters and suffixed with ``…`` so the JSON / audit field never
        splits a Chinese character mid-codepoint.

    Used by ``tools.apply_rule`` / ``tools.load_rule`` to populate the
    ``evidence_excerpt`` field of the review-comment JSON (the same field
    that ships through to the HIS 审方栏 review comment).
    """
    if not body or not query_tokens:
        return ""

    sentences = _split_sentences_zh(body)
    if not sentences:
        return ""

    top = select_top_sentence(query_tokens, sentences)
    if not top:
        return ""

    if len(top) <= max_chars:
        return top

    # Truncate by Python chars (== Unicode code points) so we never cut a
    # Chinese character in half; the ellipsis is appended outside the cut
    # so total length ≤ max_chars + 1.
    return top[:max_chars].rstrip() + "…"


def search_rules(
    query: str,
    rules: list[dict[str, Any]],
    *,
    drug_class: str | None = None,
    limit: int = 5,
    strict_drug_class: bool = False,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` rules ranked by token-overlap + severity.

    Mirrors the source product's ``agency_agents_search`` flow at
    build-hermes-plugin.py L303-328:
      1. tokenise the query;
      2. score every rule;
      3. drop rules with zero overlap;
      4. (optionally) filter by drug_class;
      5. sort by ``(-score, severity_rank, body_len_norm, rule_id)``;
      6. trim to top ``limit`` and emit trimmed summaries.

    Parameters
    ----------
    query : str
        User query (用药医嘱字段串、药品名、关键词),由 HIS 工作站传入。
    rules : list[dict]
        All rules from ``data/rules.json`` (each has rule_id / drug_class /
        severity / evidence_source / population / body / file_path).
    drug_class : str | None
        Optional hard filter; when set, only rules whose drug_class exactly
        matches are kept.  By default rules with a non-matching drug_class
        are silently dropped.  When ``strict_drug_class=True`` the same
        filter applies but a ``UserWarning`` is emitted for every excluded
        rule so the caller can see what was filtered out and why.
    limit : int
        Maximum number of results to return.
    strict_drug_class : bool
        When True, ``drug_class`` filtering is strict-equal and a
        ``UserWarning`` is emitted per excluded rule whose drug_class does
        not strictly equal the filter.  Defaults to False (silent drop).

    Returns
    -------
    list[dict]
        Each dict is a trimmed summary plus ``score``. The list is ordered
        from most-relevant to least-relevant; ties are broken by severity
        (high > medium > low), then by shorter body length (focused detail
        preferred), and finally by rule_id lexicographically so the order
        is stable across runs.
    """
    if not rules:
        return []

    if limit <= 0:
        return []

    query_tokens = _tokens_zh(query)

    # Task 30: drug-name fuzzy recall for clinical typos.  Runs once
    # per query (NOT inside _tokens_zh, which is also invoked 12*4=48
    # times per search for rule field tokenisation where the fuzzy
    # pass would be wasted work).  The fuzzy layer adds canonical drug
    # names for tokens within _MAX_EDIT_DISTANCE of any alias entry,
    # so misspelt query strings ('庆大梅素' / 'metformn') still light
    # up the correct rule body.
    if query_tokens:
        fuzzy_added: set[str] = set()
        for token in query_tokens:
            for canonical in _fuzzy_normalize(token, _DRUG_NAME_ALIASES):
                fuzzy_added.add(canonical)
        query_tokens = query_tokens | fuzzy_added

    if not query_tokens:
        # An empty / whitespace-only query has nothing to match on; return
        # an empty list rather than silently returning all rules.
        return []

    target_drug_class = drug_class.strip() if drug_class else None

    scored: list[tuple[float, dict[str, Any]]] = []
    for rule in rules:
        if target_drug_class is not None:
            rule_drug_class = str(rule.get("drug_class", "")).strip()
            if rule_drug_class != target_drug_class:
                if strict_drug_class:
                    warnings.warn(
                        f"drug_class={rule_drug_class!r} not strictly equal "
                        f"to filter {target_drug_class!r}; "
                        f"rule {rule.get('rule_id', '<unknown>')} excluded",
                        UserWarning,
                        stacklevel=2,
                    )
                continue
        score = _score_rx(query_tokens, rule)
        if score <= 0.0:
            continue
        # Severity weighting on the raw score.  high → 1.0; medium → 0.7;
        # low → 0.4.  This makes the secondary sort stable without
        # completely burying a lower-severity rule that has a much higher
        # raw overlap (e.g. an obviously-pertinent 'low' rule still
        # surfaces above an irrelevant 'high' rule).
        severity = str(rule.get("severity", "low")).lower()
        severity_weight = {  # local copy to avoid mutating constants
            "high": 1.0,
            "medium": 0.7,
            "low": 0.4,
        }.get(severity, 0.4)
        scored.append((score * severity_weight, rule))

    # Multi-key sort (Task 20):
    #   1. -score                           (highest overlap first)
    #   2. SEVERITY_RANK[severity]           (high first)
    #   3. math.tanh(len(body)/1000)         (short body first → 聚焦描述)
    #   4. rule_id                           (字典序保稳定 tie-break)
    #
    # math.tanh() squeezes body length into (0, 1) so very long bodies
    # don't dominate the sort the way raw len() would; a 1000-char body
    # scores ~0.76, a 2000-char body ~0.96, a 100-char body ~0.10.
    # Smaller tanh values sort first when the key is ascending, so short
    # bodies surface before long ones.
    def _sort_key(item: tuple[float, dict[str, Any]]) -> tuple:
        score, rule = item
        body_text = str(rule.get("body", ""))
        body_norm = math.tanh(len(body_text) / 1000.0)
        severity = str(rule.get("severity", "low")).lower()
        return (
            -score,
            SEVERITY_RANK.get(severity, 2),
            body_norm,
            str(rule.get("rule_id", "")),
        )

    scored.sort(key=_sort_key)

    return [_summary_rx(rule, score) for score, rule in scored[:limit]]


# ---------------------------------------------------------------------------
# 规则互斥检测 (detect_rule_conflicts)
# ---------------------------------------------------------------------------
# 真实审方场景下,同一 query (e.g. '庆大霉素 儿童 8 岁') 常同时命中多条规则
# (rx-aminoglycoside-pediatric + rx-aminoglycoside-tdm),药师难以一眼判定
# 该用哪条。本模块在 search_rules 返回的 top_rules 之上做互斥检测:
#   - 冲突类型 1:same_drug_class_multiple_high
#     (同 drug_class ≥2 条 high → 提示「该类已有多条 high 规则」,
#      推荐 body 最具体者:长度 + token 命中数加权打分)
#   - 冲突类型 2:contradictory_applies_to
#     (age_max 与 age_min 同时触发 / pregnancy=true 与 pregnancy=false 等,
#      提示「患者人群特征自相矛盾,请人工复核」)
#   - 冲突类型 3:cross_drug_class_aggregation
#     (不同 drug_class 但 population 描述相近,提示「多类儿童/孕妇/肾损
#      用药同时命中,请人工复核」)
# 返回 {conflicts, recommended_rule_id, has_conflict};tools.apply_rule
# 把字段暴露到 HIS 审方意见 JSON,药师可一眼看到提示与推荐。
#
# 设计取舍:
#   - 字段不引入新依赖:applies_to 在 rules_index.json / rules/*.md 中
#     已是 list[str] / list[dict] / dict 三种形态共存,parse_applies_to
#     归一为 dict[str, Any];空规则 / 缺字段 / 异常取值不阻塞 → 退化
#     为空冲突列表(避免静默抛错拖死审方主路径);
#   - recommended_rule_id 仅在「同 drug_class 多 high」+「有具体性打分」
#     场景下返回;其他两类冲突场景由药师人工选,不强制推荐;
#   - 冲突结果完全派生自 query + top_rules,不入 audit(审方意见 JSON
#     字段写入 audit,但 conflicts 是 UI 提示,不污染审计流)。


def _parse_applies_to(applies_to: Any) -> dict[str, Any]:
    """归一化 ``applies_to`` 字段为 ``{key: value}`` 字典。

    真实数据形态:
      - list[dict]: ``[{"age_max": 8}, {"pregnancy": true}]`` (index_builder 已支持)
      - list[str]: ``["age_max=8", "pregnancy=true"]`` (旧 fallback)
      - dict: ``{"age_max": 8, "pregnancy": true}`` (PyYAML 直读结果)
    任一形态都归一为 ``dict``;空 / 异常 → 空 dict。
    """
    if not applies_to:
        return {}
    if isinstance(applies_to, dict):
        return {str(k): v for k, v in applies_to.items()}
    if isinstance(applies_to, list):
        out: dict[str, Any] = {}
        for item in applies_to:
            if isinstance(item, dict):
                for k, v in item.items():
                    out[str(k)] = v
            elif isinstance(item, str):
                if "=" in item:
                    key, _, value = item.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key:
                        # best-effort 数字 / 布尔转换
                        if value.lower() in ("true", "false"):
                            out[key] = (value.lower() == "true")
                        else:
                            try:
                                out[key] = int(value)
                            except ValueError:
                                try:
                                    out[key] = float(value)
                                except ValueError:
                                    out[key] = value
                elif item.strip():
                    out[item.strip()] = True
        return out
    return {}


def _population_signature(population: str) -> str:
    """粗粒度 population 标签(儿童 / 孕妇 / 肾损 / 成人 / 老年 / 通用)。

    用于 cross_drug_class_aggregation 冲突检测:同 signature 不同 drug_class
    即视为「多类同人群用药命中」,提示人工复核。
    """
    if not population:
        return "通用"
    p = str(population)
    if "儿童" in p or "小儿" in p or "婴幼儿" in p or "未成年" in p:
        return "儿童"
    if "孕妇" in p or "妊娠" in p or "产妇" in p or "哺乳" in p:
        return "孕妇"
    if "肾" in p or "eGFR" in p or "肾损" in p or "透析" in p:
        return "肾损"
    if "老年" in p or "高龄" in p:
        return "老年"
    if "成人" in p or "成年" in p:
        return "成人"
    return "通用"


def _specificity_score(rule: dict[str, Any], query_tokens: set[str]) -> tuple:
    """Compute a (token_overlap, body_len, rule_id) tuple for recommendation ranking.

    Used by detect_rule_conflicts to pick the most specific rule among
    same_drug_class_multiple_high candidates.  The order is:
      - higher token_overlap with query_tokens wins (more relevant);
      - ties broken by longer body (more clinical detail, hence more specific);
      - final tie-break by rule_id ascending (deterministic).
    """
    body = str(rule.get("body", ""))
    body_tokens = _tokens_zh(body)
    overlap = len(query_tokens & body_tokens)
    return (-overlap, -len(body), str(rule.get("rule_id", "")))


def detect_rule_conflicts(
    query: Any,
    top_rules: list[tuple[dict[str, Any], float]],
) -> dict[str, Any]:
    """Detect conflicting rules in a top-N result set and pick a recommendation.

    Parameters
    ----------
    query : dict | str | None
        Either the order_context dict (preferred — gives access to
        patient_age / pregnancy / egfr for applies_to contradiction checks)
        or the raw query string.  ``None`` and empty inputs are accepted;
        conflict detection degrades gracefully (contradictory_applies_to
        cannot fire without applies_to data, but same_drug_class + cross-
        drug_class can).
    top_rules : list[tuple[rule, score]]
        Ordered list of (rule_dict, score) pairs (typically the output of
        search_rules before trimming; pairs already ordered by score).

    Returns
    -------
    dict with three keys:
      - ``conflicts``: ``list[dict]`` — each entry has
        ``conflict_type`` (one of ``"same_drug_class_multiple_high"``,
        ``"contradictory_applies_to"``, ``"cross_drug_class_aggregation"``),
        ``rule_ids`` (list of conflicting rule_ids), and a human-readable
        ``message``.
      - ``recommended_rule_id``: ``str | None`` — set when type-1 conflicts
        produce a clear winner (longest body + most query overlap).  ``None``
        when no recommendation can be made safely.
      - ``has_conflict``: ``bool`` — convenience flag for the REST / CLI
        "is there anything to warn about?" check.
    """
    conflicts: list[dict[str, Any]] = []

    if not top_rules:
        return {
            "conflicts": conflicts,
            "recommended_rule_id": None,
            "has_conflict": False,
        }

    rules_only = [rule for rule, _ in top_rules]

    # ------------------------------------------------------------------
    # Normalise query for token-overlap ranking.
    # ------------------------------------------------------------------
    if isinstance(query, dict):
        order_context = query
        query_token_source: list[str] = []
        for value in order_context.values():
            if isinstance(value, str):
                query_token_source.append(value)
            elif isinstance(value, (int, float)):
                query_token_source.append(str(value))
        query_tokens: set[str] = set()
        for value in query_token_source:
            query_tokens |= _tokens_zh(value)
    else:
        order_context = {}
        query_tokens = _tokens_zh(query or "")

    # ------------------------------------------------------------------
    # Conflict Type 1: same_drug_class_multiple_high
    # 同 drug_class 内 ≥2 条 high 规则 → 提示「该类已有多条 high 规则,
    # 取最具体的一条」,推荐 body 最具体者(token overlap + body 长度打分)。
    # ------------------------------------------------------------------
    by_class: dict[str, list[dict[str, Any]]] = {}
    for rule in rules_only:
        drug_class = str(rule.get("drug_class", "")).strip()
        severity = str(rule.get("severity", "")).lower()
        if drug_class and severity == "high":
            by_class.setdefault(drug_class, []).append(rule)

    recommended_rule_id: str | None = None
    for drug_class, rules_in_class in by_class.items():
        if len(rules_in_class) < 2:
            continue
        rule_ids = sorted(
            str(r.get("rule_id", "")) for r in rules_in_class
        )
        ranked = sorted(
            rules_in_class,
            key=lambda r: _specificity_score(r, query_tokens),
        )
        winner = ranked[0] if ranked else None
        winner_id = (
            str(winner.get("rule_id", "")) if winner is not None else None
        )
        conflicts.append(
            {
                "conflict_type": "same_drug_class_multiple_high",
                "rule_ids": rule_ids,
                "drug_class": drug_class,
                "message": (
                    f"drug_class='{drug_class}' 下有 {len(rules_in_class)} 条 "
                    f"high 规则同时命中;推荐最具体的一条 "
                    f"({winner_id or '无'}),其余规则建议人工复核是否适用。"
                ),
                "recommended_rule_id": winner_id,
            }
        )
        if winner_id and recommended_rule_id is None:
            recommended_rule_id = winner_id

    # ------------------------------------------------------------------
    # Conflict Type 2: contradictory_applies_to
    # 同 query 命中的多条规则,applies_to 中存在显性对立:
    #   - age_max vs age_min 同时存在但取值错位
    #     (如 rx-foo.age_max=8 + rx-bar.age_min=18 → query 同时既"儿童"又"成人")
    #   - pregnancy=true vs pregnancy=false
    #     (rx-foo 仅适用于妊娠, rx-bar 排除妊娠 → query 自相矛盾)
    #   - renal_function=normal vs renal_function=impairment
    # ------------------------------------------------------------------
    normalised_applies: list[tuple[str, dict[str, Any]]] = []
    for rule in rules_only:
        rid = str(rule.get("rule_id", ""))
        applies_raw = rule.get("applies_to", [])
        normalised_applies.append((rid, _parse_applies_to(applies_raw)))

    def _find_pair_conflict(key: str, predicate) -> list[str]:
        """Return rule_ids whose applies_to[key] satisfies the predicate."""
        matched: list[str] = []
        for rid, applies in normalised_applies:
            if key not in applies:
                continue
            try:
                if predicate(applies[key]):
                    matched.append(rid)
            except (TypeError, ValueError):
                continue
        return matched

    contradictions: list[tuple[str, list[str], list[str]]] = []

    # 2a. pregnancy 矛盾
    preg_true = _find_pair_conflict(
        "pregnancy",
        lambda v: v is True or (isinstance(v, str) and v.lower() == "true"),
    )
    preg_false = _find_pair_conflict(
        "pregnancy",
        lambda v: v is False or (isinstance(v, str) and v.lower() == "false"),
    )
    if preg_true and preg_false:
        contradictions.append(
            ("pregnancy_true_vs_false", preg_true, preg_false)
        )

    # 2b. renal_function 矛盾
    renal_normal = _find_pair_conflict(
        "renal_function",
        lambda v: v == "normal"
        or (isinstance(v, str) and v.lower() == "normal"),
    )
    renal_impaired = _find_pair_conflict(
        "renal_function",
        lambda v: v in ("impairment", "impaired", "low", "severe")
        or (
            isinstance(v, str)
            and v.lower() in ("impairment", "impaired", "low", "severe")
        ),
    )
    if renal_normal and renal_impaired:
        contradictions.append(
            ("renal_function_normal_vs_impaired", renal_normal, renal_impaired)
        )

    # 2c. age 矛盾: max<=14(儿童限) + min>=18(成人限)
    age_max_kids = _find_pair_conflict(
        "age_max", lambda v: isinstance(v, (int, float)) and v <= 14
    )
    age_min_adults = _find_pair_conflict(
        "age_min", lambda v: isinstance(v, (int, float)) and v >= 18
    )
    if age_max_kids and age_min_adults:
        contradictions.append(
            ("age_child_limit_vs_adult_min", age_max_kids, age_min_adults)
        )

    for ctype, side_a, side_b in contradictions:
        rule_ids = sorted(set(side_a) | set(side_b))
        conflicts.append(
            {
                "conflict_type": "contradictory_applies_to",
                "rule_ids": rule_ids,
                "subtype": ctype,
                "message": (
                    f"applies_to 子条件 {ctype!r} 在多条规则中互斥:"
                    f"一侧为 {side_a},另一侧为 {side_b};"
                    "query 患者人群特征自相矛盾,请人工复核。"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Conflict Type 3: cross_drug_class_aggregation
    # 不同 drug_class 但 population 签名一致(儿童 / 孕妇 / 肾损等)
    # → 提示「多类同人群用药同时命中,需人工核对总药物负担」。
    # ------------------------------------------------------------------
    by_pop_signature: dict[str, list[str]] = {}
    for rule in rules_only:
        rid = str(rule.get("rule_id", ""))
        if not rid:
            continue
        sig = _population_signature(str(rule.get("population", "")))
        if sig in ("通用", ""):
            continue
        by_pop_signature.setdefault(sig, []).append(rid)

    for sig, rule_ids in by_pop_signature.items():
        drug_classes_for_sig = sorted(
            {
                str(r.get("drug_class", "")).strip()
                for r in rules_only
                if str(r.get("rule_id", "")) in rule_ids
                and str(r.get("drug_class", "")).strip()
            }
        )
        if len(drug_classes_for_sig) < 2:
            continue
        if len(rule_ids) < 2:
            continue
        conflicts.append(
            {
                "conflict_type": "cross_drug_class_aggregation",
                "rule_ids": sorted(rule_ids),
                "population_signature": sig,
                "drug_classes": drug_classes_for_sig,
                "message": (
                    f"population 签名='{sig}' 在 {len(drug_classes_for_sig)} 个 "
                    f"drug_class 同时命中("
                    f"{', '.join(drug_classes_for_sig)}),"
                    "请人工核对患者总药物负担。"
                ),
            }
        )

    return {
        "conflicts": conflicts,
        "recommended_rule_id": recommended_rule_id,
        "has_conflict": bool(conflicts),
    }
