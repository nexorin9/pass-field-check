"""规则 Markdown 的解析与索引构建基础。

源码产品能力参考：github_ref/agency-agents/scripts/build-hermes-plugin.py
  - parse_agent (L37-67) → 复用其 frontmatter 切分思路（--- 切分 + key:value 逐行），
    改写为药品审方领域 parse_rx_rule。
  - collect_agents (L70-91) → 复用其全目录扫描与去重 raise 思路，
    改写为 collect_rx_rules（以 rule_id 为去重键）。
  - slugify (L31-34) → 复用其 lowercase + 非 alphanumeric 替换为 - 的思路，
    改写为药品审方上下文下的轻量工具。
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = {"high", "medium", "low"}
_REQUIRED_FIELDS = ("drug_class", "evidence_source", "population")
_RULE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return parsed frontmatter and the body from a rule document.

    The first ``---`` is the optional frontmatter opener. A closing delimiter
    is optional for the lightweight fallback, but a malformed YAML block is
    still reparsed manually so a simple ``|`` value can be recovered.
    """
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        # A rule without frontmatter can still be used by the lightweight
        # parser.  Its metadata is everything before the first Markdown
        # heading; the heading and following text become the body.
        heading = next((i for i, line in enumerate(lines) if line.lstrip().startswith("#")), len(lines))
        raw_meta = "\n".join(lines[:heading]).strip()
        body = "\n".join(lines[heading:]).strip()
        parsed: dict[str, Any] | None = _parse_yaml_light(raw_meta) if raw_meta else None
        return parsed, body

    start = 1
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == "---"), len(lines))
    raw_frontmatter = "\n".join(lines[start:end])
    body = "\n".join(lines[end + 1 :]).strip()
    try:
        loaded = yaml.safe_load(raw_frontmatter)
    except (yaml.YAMLError, ValueError, TypeError):
        loaded = _parse_yaml_light(raw_frontmatter)
    if loaded is not None and not isinstance(loaded, dict):
        raise ValueError("frontmatter 必须是对象")
    return loaded, body


def _parse_yaml_light(raw: str) -> dict[str, Any]:
    """Parse the small YAML subset used by fallback rule files.

    It supports scalar ``key: value`` entries, quoted values, and ``|`` block
    scalars. Unknown keys are intentionally retained for forward compatibility.
    """
    result: dict[str, Any] = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace():
            raise ValueError(f"无法解析 YAML-light 内容：{line!r}")
        if ":" not in line:
            raise ValueError(f"无法解析 YAML-light 行：{line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError("YAML-light 字段名不能为空")
        if value == "|":
            collected: list[str] = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                if lines[i].strip():
                    collected.append(lines[i].strip())
                i += 1
            result[key] = "\n".join(collected)
            continue
        if value:
            result[key] = _parse_scalar(value)
        else:
            # A nested mapping/list is not part of the compatibility subset;
            # leave it to the main YAML parser rather than losing the field.
            result[key] = ""
        i += 1
    return result


def _parse_scalar(value: str) -> Any:
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _normalise_applies_to(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [f"{key}={item}" for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def parse_rx_rule(path: Path) -> dict[str, Any]:
    """Parse one ``rx-*.md`` rule into the stable runtime record shape."""
    rule_path = Path(path)
    metadata, body = _split_frontmatter(rule_path.read_text(encoding="utf-8-sig"))
    if metadata is None:
        raise ValueError(f"{rule_path}: 缺少 frontmatter")

    rule_id = metadata.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id or re.fullmatch(r"[a-z0-9][a-z0-9-]*", rule_id) is None:
        raise ValueError(f"{rule_path}: rule_id 缺失或格式非法: {rule_id!r}")

    severity = metadata.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(f"{rule_path}: severity 必须是 high/medium/low，实际为 {severity!r}")

    for field in _REQUIRED_FIELDS:
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError(f"{rule_path}: 缺少必填字段 {field}")

    # YAML-light fixtures may carry a body scalar without a closing frontmatter
    # delimiter. Keep that body in the returned record as a compatibility aid.
    if isinstance(metadata.get("body"), str) and metadata["body"].strip() and metadata["body"].strip() not in body:
        body = f"{metadata['body'].strip()}\n\n{body}".strip()

    # Body 必填且非空:无 body 即视为规则未写完,直接拒绝以避免静默通过。
    if not body.strip():
        raise ValueError(f"{rule_path}: body 缺失或为空,无法作为审方依据")

    # 未知字段保留(便于 forward-compat):仅保留标量字段,跳过 list/dict,
    # 因为 applies_to 等结构化字段已由 _normalise_applies_to 归一。
    known_fields = {
        "rule_id",
        "drug_class",
        "severity",
        "evidence_source",
        "population",
        "applies_to",
        "body",
    }
    extras: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in known_fields:
            continue
        if isinstance(value, (list, dict)):
            # 结构化字段不内嵌进 extras,避免破坏 record 形状。
            continue
        extras[key] = value

    record: dict[str, Any] = {
        "rule_id": rule_id,
        "drug_class": metadata["drug_class"],
        "severity": severity,
        "evidence_source": metadata["evidence_source"],
        "population": metadata["population"],
        "applies_to": _normalise_applies_to(metadata.get("applies_to", [])),
        "body": body,
    }
    if extras:
        record["extras"] = extras
    return record


def slugify(value: str) -> str:
    """Return a kebab-case slug for the given text.

    参考：build-hermes-plugin.py L31-34 slugify 思路
      (lowercase + 非 alphanumeric 替换为 - + 收尾 strip)。
    本实现保留中文与连续短横线压缩，与院内脚本对目录名/dataset 命名习惯一致。
    """
    if value is None:
        return ""
    lowered = str(value).strip().lower()
    # Replace any non-[a-z0-9] run with a single dash, then strip leading/trailing dashes.
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-")


def load_categories(index_path: Path) -> list[str]:
    """Read ``rules_index.json`` and return the categories list.

    The single-source-of-truth ``rules_index.json`` is the categories registry
    used by ``collect_rx_rules`` to validate rule ``drug_class`` values. If the
    ``categories`` field is missing we raise -- silent defaults have caused
    catalog drift in similar projects (see build-hermes-plugin.py:21-28 about
    reading ``divisions.json`` rather than hardcoding the division list).
    """
    path = Path(index_path)
    if not path.exists():
        raise ValueError(f"{path}: rules_index.json 不存在")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError(f"{path}: 缺少 categories 字段或为空")
    return [str(item) for item in categories]


def collect_rx_rules(
    rules_dir: Path,
    *,
    categories: list[str] | None = None,
    strict_categories: bool = False,
) -> list[dict[str, Any]]:
    """Walk ``rules_dir`` and return parsed rx rules, deduped by ``rule_id``.

    参考：build-hermes-plugin.py L70-91 collect_agents 思路
      (rglob *.md → 逐条 parse → 按 slug 去重 raise SystemExit)。
    本实现改写为：
      - 文件名匹配 ``rx-*.md``（避免后续 ``audit-*.md`` 等元数据被误纳入）。
      - 去重键为 ``rule_id``（审方语义下更稳定，比文件名更不易因重命名而失同步）。
      - 类别校验默认 warn、不阻塞（药事委员会扩展类别是常见运营动作，不应让索引构建失败）。
      - 返回值附 ``file_path``（绝对路径字符串），便于 audit 阶段溯源。
    """
    root = Path(rules_dir)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"{root}: 规则目录不存在")

    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("rx-*.md")):
        if not path.is_file():
            continue
        record = parse_rx_rule(path)
        record["file_path"] = str(path.resolve())
        rule_id = record["rule_id"]
        if rule_id in records:
            existing = records[rule_id]["file_path"]
            raise ValueError(
                f"rule_id duplicate: {rule_id!r} 已存在于 {existing}，"
                f"同时出现在 {path}"
            )
        records[rule_id] = record

    if categories is not None:
        for rule_id, record in records.items():
            drug_class = record.get("drug_class")
            if drug_class not in categories:
                message = (
                    f"规则 {rule_id} (file={record['file_path']}) 的 drug_class="
                    f"{drug_class!r} 不在 rules_index.json 的 categories 清单中"
                )
                if strict_categories:
                    raise ValueError(message)
                warnings.warn(message, UserWarning, stacklevel=2)

    # Stable order by rule_id for downstream tooling (audit, REST responses).
    return [records[key] for key in sorted(records)]
