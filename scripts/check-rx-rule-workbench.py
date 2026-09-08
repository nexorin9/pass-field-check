#!/usr/bin/env python3
"""Validate the pass-field-check workbench against its 4-tool contract.

源码产品能力参考：github_ref/agency-agents/scripts/check-hermes-plugin.py
  - ``RecordingContext`` (L24-29) → mirror the same mock context here; it only
    needs ``register_tool(**kwargs)`` and stores each registration so we can
    inspect names / schema / handler.
  - ``builder.build(...)`` + ``plugin.register(ctx)`` (L33-44) → replaced by
    in-process calls: we read ``rules_index.json`` + ``rules/*.md`` via
    ``index_builder.collect_rx_rules``, then ``bind_runtime(rules)`` so the
    handler closures see the live rules list, then ``register_rx_tools(ctx)``.
  - 4-tool names assertion + per-schema validation (L46-62) → ported as-is,
    changing ``agency_agents_*`` to ``rx_rule_*``.
  - search→inspect chain smoke (L64-75) → reuse top-1 ``rule_id`` as the
    inspect input; the inspect response must echo the same ``rule_id``.

Fusion-time differences vs the source product:
  - We don't ship a compiled plugin directory; the smoke loads the rules at
    startup so the run also exercises the index builder.
  - We always run from the project root (``pass-field-check/``) regardless of
    the caller's cwd, by anchoring on ``Path(__file__).resolve().parents[1]``.
  - Failure prints a step-tagged message and exits non-zero so a CI shell
    harness can capture it; success prints ``ok`` (per the Phase 4 task spec).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Make ``pass_field_check`` importable when the smoke is launched directly
# via ``python scripts/check-rx-rule-workbench.py`` without an editable install.
# tools.py uses ``from .runtime import ...`` so the package context is required.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pass_field_check import index_builder, tools as tools_mod  # noqa: E402


class RecordingContext:
    """Minimal stand-in for ``HermesPluginContext``.

    Mirrors ``check-hermes-plugin.py:RecordingContext``: just records each
    ``register_tool(**kwargs)`` call so the smoke can introspect names,
    schemas, and handlers.
    """

    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(self, **kwargs: Any) -> None:
        name = kwargs.get("name")
        if not name:
            raise RuntimeError("register_tool called without name")
        self.tools[name] = kwargs


def _die(step: str, message: str) -> None:
    """Print a tagged failure and exit non-zero."""
    sys.stderr.write(f"FAIL [{step}]: {message}\n")
    sys.stderr.flush()
    raise SystemExit(1)


def _assert(condition: bool, step: str, message: str) -> None:
    if not condition:
        _die(step, message)


def main() -> int:
    # ------------------------------------------------------------------
    # Step 1: walk rules/*.md and build the in-memory rules list (mirrors
    # the source product's builder.build() output but in-process). The
    # modules are imported at module load time so ``pass_field_check``
    # is a proper package context (needed for ``from .runtime import ...``).
    # ------------------------------------------------------------------
    rules_dir = REPO_ROOT / "rules"
    index_path = REPO_ROOT / "rules_index.json"

    try:
        categories = index_builder.load_categories(index_path)
        rules = index_builder.collect_rx_rules(rules_dir, categories=categories)
    except Exception as exc:
        _die("load-rules", f"{type(exc).__name__}: {exc}")

    _assert(len(rules) >= 12, "load-rules", f"expected >=12 rules, got {len(rules)}")

    # Bind the rules into tools.py's module-level state so handler closures
    # see them (mirror of how cli.py / api.py bootstrap).
    tools_mod.bind_runtime(rules, audit_path=None)

    # ------------------------------------------------------------------
    # Step 3: register the 4 tools and assert names + per-schema shape.
    # ------------------------------------------------------------------
    ctx = RecordingContext()
    tools_mod.register_rx_tools(ctx)

    expected_tools = {
        "rx_rule_search",
        "rx_rule_inspect",
        "rx_rule_load",
        "rx_rule_apply",
    }
    _assert(
        set(ctx.tools) == expected_tools,
        "tool-names",
        f"expected {sorted(expected_tools)}, got {sorted(ctx.tools)}",
    )

    for name, registration in ctx.tools.items():
        schema = registration.get("schema")
        _assert(schema is not None, f"schema-{name}", "schema is missing")
        _assert(schema.get("name") == name, f"schema-{name}", "schema.name mismatch")
        _assert(
            isinstance(schema.get("description"), str) and schema["description"],
            f"schema-{name}",
            "schema.description is missing",
        )
        parameters = schema.get("parameters")
        _assert(isinstance(parameters, dict), f"schema-{name}", "parameters missing")
        _assert(
            parameters.get("type") == "object",
            f"schema-{name}",
            "parameters.type must be object",
        )
        _assert(
            isinstance(parameters.get("properties"), dict),
            f"schema-{name}",
            "parameters.properties must be a dict",
        )
        _assert(
            isinstance(parameters.get("required"), list),
            f"schema-{name}",
            "parameters.required must be a list",
        )
        _assert(
            callable(registration.get("handler")),
            f"handler-{name}",
            "handler must be callable",
        )

    # ------------------------------------------------------------------
    # Step 4: search→inspect chain smoke (mirrors check-hermes-plugin.py:64-75).
    # ------------------------------------------------------------------
    search_handler = ctx.tools["rx_rule_search"]["handler"]
    inspect_handler = ctx.tools["rx_rule_inspect"]["handler"]

    search_result = search_handler({"query": "庆大霉素 儿童"})
    _assert(search_result.get("success") is True, "search", f"got {search_result!r}")
    results = search_result.get("results") or []
    _assert(results, "search", "expected at least one hit for '庆大霉素 儿童'")
    top_rule_id = results[0].get("rule_id")
    _assert(
        isinstance(top_rule_id, str) and top_rule_id,
        "search",
        f"top result missing rule_id: {results[0]!r}",
    )

    inspected = inspect_handler({"rule_id": top_rule_id})
    _assert(inspected.get("success") is True, "inspect", f"got {inspected!r}")
    rule_payload = inspected.get("rule") or {}
    _assert(
        rule_payload.get("rule_id") == top_rule_id,
        "inspect",
        f"inspect.rule.rule_id ({rule_payload.get('rule_id')}) "
        f"!= search top rule_id ({top_rule_id})",
    )
    _assert(
        isinstance(inspected.get("body"), str) and inspected["body"],
        "inspect",
        "inspect.body must be non-empty when include_body defaults to True",
    )

    # ------------------------------------------------------------------
    # Step 5: success.
    # ------------------------------------------------------------------
    summary = {
        "rules_loaded": len(rules),
        "tools_registered": sorted(ctx.tools),
        "search_top_rule_id": top_rule_id,
    }
    sys.stdout.write(f"ok: pass-field-check workbench smoke passed ({json.dumps(summary, ensure_ascii=False)})\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
