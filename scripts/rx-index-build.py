#!/usr/bin/env python3
"""Build the JSON index for the 用药字段对照工作台 (pass-field-check).

This script walks ``rules_dir`` for ``rx-*.md`` rule files, parses each rule's
frontmatter, dedupes by ``rule_id`` and emits a single ``data/rules.json``
file that the runtime (CLI / REST / audit) reads from.

源码产品能力参考（参考地基 / evidence chain）
=============================================
源 repo：github_ref/agency-agents/scripts/build-hermes-plugin.py

复用的源产品主路径：
  - L21-28 ``division_dirs(repo_root)`` — 从单一 ``divisions.json`` 派生类别
    清单的思路。改写为本脚本中读 ``rules_index.json`` 的 ``categories`` 字段
    （**单一事实来源**：类别新增须走药事委员会流程，避免硬编码漂移）。
  - L31-34 ``slugify`` — lowercase + 非 alphanumeric 替换为 -。本脚本不直接
    调用，但 ``pass_field_check.index_builder.slugify`` 已沿用同思路。
  - L70-91 ``collect_agents(repo_root)`` — 核心主路径：**rglob *.md →
    逐个 parse → 按 slug 去重 raise SystemExit**。
    改写为本脚本的 ``collect_rx_rules(rules_dir, categories=...)``（见
    ``pass_field_check/index_builder.py``）：
      - 入口从 repo_root + division_dirs 改写为 rules_dir（单一目录）；
      - 解析从 ``parse_agent`` 改写为 ``parse_rx_rule``（含 severity 与
        drug_class / evidence_source / population 必填校验）；
      - 去重键从 ``slug`` 改写为 ``rule_id``（药品审方语义下更稳定，比文件名
        更不易因重命名而失同步）；
      - 类别校验默认 warn、不阻塞（药事委员会扩展类别是常见运营动作）。

融合后产品主路径（本脚本）
==========================
``rules_index.json`` (version + categories) + ``rules/*.md`` →
``pass_field_check.index_builder.collect_rx_rules`` →
``data/rules.json`` (顶层 version + rules 数组)。

写入采用 *tmp → fsync → rename* 原子模式：避免构建过程中读进程看到半截
JSON；与 ``build-hermes-plugin.py`` 在 Hermes 启动时加载 ``data/agents.json``
的契约一致（启动期数据半截 = 启动失败）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本既可 ``python scripts/rx-index-build.py`` 跑，也可
# ``python -m scripts.rx_index_build`` 跑（后续 task 9 cli.py 会复用 main）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pass_field_check.index_builder import (  # noqa: E402  (sys.path tweak above)
    collect_rx_rules,
    load_categories,
)


def _load_index_version(index_path: Path) -> str:
    """Return the top-level ``version`` field from ``rules_index.json``.

    The version is bumped when the rule catalog is republished; the runtime
    reads it back to confirm the on-disk JSON was produced by a build with the
    same category snapshot.
    """
    payload = json.loads(Path(index_path).read_text(encoding="utf-8-sig"))
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{index_path}: 缺少 version 字段")
    return version


def _atomic_write_json(out_path: Path, payload: dict) -> None:
    """Write ``payload`` to ``out_path`` via a ``.tmp`` + ``rename`` atomic swap.

    Pattern matches the "write-then-rename" idiom used by long-running
    loaders (Hermes plugin init, package managers) so a reader never sees a
    half-written file.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        # fsync ensures the bytes are on disk before rename; on POSIX the
        # rename itself is atomic at the directory entry level.
        import os as _os  # local import keeps the top of the module tidy
        _os.fsync(fh.fileno())
    tmp_path.replace(out_path)


def build_index(
    rules_dir: Path,
    index_path: Path,
    *,
    categories: list[str] | None,
    strict_categories: bool,
) -> dict:
    """Collect rules + load version, returning the payload to be persisted.

    Kept separate from CLI parsing so the function is unit-testable in
    ``tests/test_index.py`` without spinning up subprocesses.
    """
    version = _load_index_version(index_path)
    if categories is None:
        # Default to the on-disk registry. Pass-through to collect_rx_rules so
        # the warn-vs-raise flag continues to behave consistently.
        categories = load_categories(index_path)
    rules = collect_rx_rules(
        rules_dir,
        categories=categories,
        strict_categories=strict_categories,
    )
    return {
        "version": version,
        "rules": rules,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rx-index-build",
        description="构建用药字段对照工作台的 data/rules.json 索引。",
    )
    parser.add_argument(
        "--rules-dir",
        type=Path,
        default=Path("./rules"),
        help="rx-*.md 规则目录（默认 ./rules）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./data/rules.json"),
        help="输出 JSON 索引路径（默认 ./data/rules.json）",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("./rules_index.json"),
        help="rules_index.json 单源类别清单路径（默认 ./rules_index.json）",
    )
    parser.add_argument(
        "--strict-categories",
        action="store_true",
        default=False,
        help="若规则的 drug_class 不在 rules_index.json 的 categories 中，则视为错误并退出非零（默认仅 warn）。",
    )
    args = parser.parse_args(argv)

    try:
        payload = build_index(
            args.rules_dir,
            args.index,
            categories=None,
            strict_categories=args.strict_categories,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _atomic_write_json(args.out, payload)
    print(
        f"ok: 写入 {args.out}（version={payload['version']}，"
        f"rules={len(payload['rules'])}）",
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())