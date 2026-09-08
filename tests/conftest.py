"""Pytest fixtures shared across tests/.

load_fixture(name):
    - 读 tests/fixtures/<name>.json (utf-8)
    - 校验顶层 _deident_note 字段存在且非空(强制脱敏声明)
    - 返回解析后的 dict

mock 替身边界(spec.md ## 对接层 显式声明):
    tests/fixtures/ 下的 mock_prescriptions.json / mock_his_fields.json 仅用于
    pytest,不作最终 HIS integration surface。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by basename and require the top-level _deident_note field.

    Args:
        name: fixture basename without extension, e.g. 'mock_prescriptions'.

    Returns:
        Parsed JSON dict.

    Raises:
        FileNotFoundError: if fixture file does not exist.
        ValueError: if _deident_note field is missing or empty.
    """
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    note = data.get("_deident_note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError(
            f"fixture {name} missing top-level _deident_note field; "
            "all fixtures must carry a de-identification disclaimer."
        )

    return data


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Return the absolute path of tests/fixtures/."""
    return FIXTURES_DIR