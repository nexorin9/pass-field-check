"""Smoke checks for ``scripts/check-rx-rule-workbench.py``.

These tests run the script as a subprocess (happy path) and also exercise
the failure path by mutating ``RX_TOOL_SCHEMAS`` so one schema is malformed
and re-importing the smoke module -- this mirrors the task's "故意构造
schema 损坏" verification step without writing the broken schema to disk.

The smoke is the same script ``@phase4-execution`` calls as the final
acceptance of Task 12, so failures here must fail loudly.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SMOKE_PATH = SCRIPTS_DIR / "check-rx-rule-workbench.py"


def _load_smoke_module():
    """Re-import the smoke script as a module so tests can monkey-patch it."""
    spec = importlib.util.spec_from_file_location(
        "check_rx_rule_workbench_under_test", SMOKE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_happy_path_exits_zero_and_prints_ok() -> None:
    """End-to-end: launching the script exits 0 and stdout contains ``ok``."""
    result = subprocess.run(
        [sys.executable, str(SMOKE_PATH)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "ok" in result.stdout, f"stdout missing 'ok': {result.stdout!r}"
    # The smoke summary mentions the 4 tool names + the golden search hit.
    for needle in (
        "rx_rule_search",
        "rx_rule_inspect",
        "rx_rule_load",
        "rx_rule_apply",
        "rx-aminoglycoside-pediatric",
        "rules_loaded",
    ):
        assert needle in result.stdout, f"stdout missing {needle!r}: {result.stdout!r}"


def test_smoke_summary_payload_is_valid_json() -> None:
    """The trailing ``(...)`` summary should parse as JSON."""
    result = subprocess.run(
        [sys.executable, str(SMOKE_PATH)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip().splitlines()[-1]
    assert line.startswith("ok: "), f"unexpected stdout prefix: {line!r}"
    # Format: "ok: <description> ({...})"
    open_paren = line.rfind("(")
    close_paren = line.rfind(")")
    assert open_paren != -1 and close_paren != -1 and close_paren > open_paren, (
        f"could not locate (...) JSON wrapper in: {line!r}"
    )
    payload = json.loads(line[open_paren + 1 : close_paren])
    assert payload["rules_loaded"] >= 12
    assert set(payload["tools_registered"]) == {
        "rx_rule_search",
        "rx_rule_inspect",
        "rx_rule_load",
        "rx_rule_apply",
    }
    assert payload["search_top_rule_id"] == "rx-aminoglycoside-pediatric"


def test_smoke_fails_when_a_tool_name_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupt one schema's ``name`` field and assert exit code != 0.

    Mirrors the task step "故意构造 schema 损坏(临时改一个 tool name)→ 断言
    exit code != 0". Mutates the live ``RX_TOOL_SCHEMAS`` tuple entry so the
    on-disk file is never touched, then re-imports the smoke module fresh
    so the mutated schema is picked up.
    """
    from pass_field_check import tools as tools_mod

    original_schemas = tools_mod.RX_TOOL_SCHEMAS
    try:
        broken = []
        for schema in original_schemas:
            if schema["name"] == "rx_rule_search":
                mutated = dict(schema)
                mutated["name"] = "rx_rule_search_BAD"
                broken.append(mutated)
            else:
                broken.append(schema)
        # RX_TOOL_SCHEMAS is a tuple; replace the module attribute with a new
        # tuple so register_rx_tools sees the broken name.
        monkeypatch.setattr(tools_mod, "RX_TOOL_SCHEMAS", tuple(broken))
        # Also patch the schema dict the registration loop references -- since
        # register_rx_tools reads the live module-level RX_*_SCHEMA constants
        # directly, mutate RX_SEARCH_SCHEMA in place.
        monkeypatch.setitem(
            tools_mod.RX_SEARCH_SCHEMA, "name", "rx_rule_search_BAD"
        )

        smoke = _load_smoke_module()
        with pytest.raises(SystemExit) as excinfo:
            smoke.main()
        assert excinfo.value.code != 0, "smoke should exit non-zero on bad schema"
    finally:
        # Restore for subsequent tests even if monkeypatch didn't tear down.
        tools_mod.RX_SEARCH_SCHEMA["name"] = "rx_rule_search"


def test_smoke_fails_when_tool_names_set_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing ``register_rx_tools`` with a no-op should fail the smoke.

    ``register_rx_tools`` reads ``RX_SEARCH_SCHEMA`` etc. directly (not the
    ``RX_TOOL_SCHEMAS`` tuple), so trimming the tuple is invisible to the
    registration. Instead we patch ``register_rx_tools`` to register zero
    tools, which triggers the "expected_tools" guard in the smoke.
    """
    from pass_field_check import tools as tools_mod

    def empty_register(_ctx):  # pragma: no cover - monkeypatched path
        return None

    monkeypatch.setattr(tools_mod, "register_rx_tools", empty_register)

    smoke = _load_smoke_module()
    with pytest.raises(SystemExit) as excinfo:
        smoke.main()
    assert excinfo.value.code != 0


def test_smoke_fails_when_rules_dir_is_missing(tmp_path: Path) -> None:
    """Empty rules directory must trigger the ``>=12 rules`` guard."""
    smoke = _load_smoke_module()
    fake_root = tmp_path / "fake-project"
    fake_root.mkdir()
    (fake_root / "rules_index.json").write_text(
        json.dumps({"version": "2026.09.08", "categories": ["抗菌药"]}),
        encoding="utf-8",
    )
    (fake_root / "rules").mkdir()
    (fake_root / "pass_field_check").mkdir()

    original_root = smoke.REPO_ROOT
    smoke.REPO_ROOT = fake_root
    try:
        with pytest.raises(SystemExit) as excinfo:
            smoke.main()
        assert excinfo.value.code != 0
    finally:
        smoke.REPO_ROOT = original_root
