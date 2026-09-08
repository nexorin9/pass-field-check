#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 pyproject.toml 中声明的 console_scripts 入口点可被 importlib.metadata 加载。

期望声明:
    [project.scripts]
    pass-fc = "pass_field_check.cli:main"

校验流程:
  Step 1 (load-entry-points): entry_points(group='console_scripts') 列出全部入口点
  Step 2 (find-pass-fc):     断言 name='pass-fc' 存在
  Step 3 (verify-target):    value 形如 'module:attr', 解析为 (module, attr)
  Step 4 (load-module):      importlib.import_module(module) 验证模块可加载
  Step 5 (resolve-attr):     importlib.import_module 找到 attr,验证 callable

任意步骤失败: 打印 FAIL [step-tag]: message + SystemExit(1)
全通过:       打印 ok: pass-fc entry point loaded → pass_field_check.cli:main + exit 0

Usage:
    python scripts/check-entry-point.py
"""
from __future__ import annotations

import importlib
import sys
from importlib.metadata import entry_points
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAME = "pass-fc"
EXPECTED_MODULE = "pass_field_check.cli"
EXPECTED_ATTR = "main"


def _die(step: str, message: str) -> None:
    print(f"FAIL [{step}]: {message}", file=sys.stderr)
    raise SystemExit(1)


def _assert(condition: bool, step: str, message: str) -> None:
    if not condition:
        _die(step, message)


def main() -> int:
    # Step 1
    try:
        eps = entry_points(group="console_scripts")
    except Exception as exc:
        _die("load-entry-points", f"entry_points(group='console_scripts') raised: {exc}")

    # Step 2: find name='pass-fc'
    matches = [e for e in eps if e.name == EXPECTED_NAME]
    _assert(
        matches,
        "find-pass-fc",
        f"no console_script entry point named '{EXPECTED_NAME}' "
        f"(found {len(eps)} console_scripts; declared in pyproject.toml [project.scripts])",
    )
    ep = matches[0]

    # Step 3: parse 'module:attr'
    value = ep.value
    _assert(
        ":" in value,
        "verify-target",
        f"entry point value '{value}' not in 'module:attr' form",
    )
    module_name, _, attr_name = value.partition(":")
    _assert(
        module_name == EXPECTED_MODULE and attr_name == EXPECTED_ATTR,
        "verify-target",
        f"entry point value '{value}' != '{EXPECTED_MODULE}:{EXPECTED_ATTR}' "
        f"(pyproject.toml [project.scripts] 与实际安装不一致 — 需重新 pip install -e .)",
    )

    # Step 4: load module
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        _die("load-module", f"importlib.import_module({module_name!r}) raised: {exc}")

    # Step 5: resolve attr
    _assert(
        hasattr(mod, attr_name),
        "resolve-attr",
        f"module {module_name!r} has no attribute {attr_name!r}",
    )
    target = getattr(mod, attr_name)
    _assert(
        callable(target),
        "resolve-attr",
        f"{module_name}.{attr_name} is not callable (got {type(target).__name__})",
    )

    print(
        f"ok: {EXPECTED_NAME} entry point loaded → "
        f"{module_name}:{attr_name} ({type(target).__name__})"
    )
    return 0


if __name__ == "__main__":
    # 保证从项目根目录运行,避免 CWD 漂移
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
