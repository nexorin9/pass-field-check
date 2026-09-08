"""CLI 主入口:pass-fc 命令的 argparse 实现。

源码产品能力参考(参考地基 / evidence chain):
  github_ref/agency-agents/scripts/build-hermes-plugin.py
    - argparse 子命令模板 → 复用其 add_subparsers + 子命令独立 argparse
      形态,改写为医院审方语义(search / inspect / load / apply)。
    - 启动期 bind_runtime(closure-over-module-state) → mirror 源产品
      写法;rules 与 audit_path 在 main() 顶部注入。
  github_ref/agency-agents/scripts/check-hermes-plugin.py
    - 退出码契约(0 = ok,非零 = fail) → 与 Hermes smoke 烟测一致,便于
      shell/CI 捕获。

融合后产品主路径(从 readme/工作流推导):
  pass-fc index-build
    → rules/*.md → collect_rx_rules → data/rules.json
  pass-fc search 'query'
    → data/rules.json → search_rules → top-N 适用规则 (人类可读表格)
  pass-fc inspect --rule-id <id>
    → inspect_rule → 规则全文 JSON
  pass-fc load --rule-id <id> --order-context '<json>'
    → load_rule → 审方草稿 JSON(rule + order_context + evidence_excerpt)
  pass-fc apply --rule-id <id> --order-context '<json>' --operator <id>
    → apply_rule → 审方意见 JSON(rule_id + order_hash + session_id +
       operator + applied_at + confirmed=False) + audit.jsonl 留痕
  pass-fc audit-export --month YYYY-MM
    → audit.jsonl 过滤当月 → CSV(utf-8-sig BOM,Excel 可直开)
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .index_builder import collect_rx_rules, load_categories
from .runtime import search_rules
from .tools import apply_rule, inspect_rule, load_rule
from . import audit as _audit_mod


# ---------------------------------------------------------------------------
# ANSI 颜色常量 (task 27)
# ---------------------------------------------------------------------------
# 颜色码仅在 use_color=True 时启用;--no-color 全局开关或非 TTY 自动关闭。
# 颜色按 severity 分级:
#   high   → 红 (严重警示)
#   medium → 黄 (提示)
#   low    → 绿 (低风险提示)


_ANSI_RESET = "\033[0m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_GREEN = "\033[32m"


def _severity_color_code(severity: str) -> str:
    """Map severity string to ANSI color code; empty code if unknown."""
    return {
        "high": _ANSI_RED,
        "medium": _ANSI_YELLOW,
        "low": _ANSI_GREEN,
    }.get(str(severity).lower(), _ANSI_RESET)


def _colorize(text: str, color_code: str, enabled: bool) -> str:
    """Wrap text in ANSI color codes unless colors are disabled."""
    if not enabled or not color_code or color_code == _ANSI_RESET:
        return text
    return f"{color_code}{text}{_ANSI_RESET}"


# ---------------------------------------------------------------------------
# 统一错误输出 (task 27)
# ---------------------------------------------------------------------------


def _emit_error(code: str, message: str, *, detail: Any = None) -> None:
    """Emit a structured JSON error to stderr for shell / CI capture.

    输出格式:
        ``{"success": false, "error": {"code": ..., "message": ...,
        "detail": ...}}``

    ``message`` 字段保留原始人类可读字符串,便于现有依赖 stderr 关键词
    检索的脚本(``grep 未找到`` / ``grep audit log``)继续工作。
    """
    payload: dict[str, Any] = {"success": False, "error": {"code": code, "message": message}}
    if detail is not None:
        payload["error"]["detail"] = detail
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# yaml-like 输出 (task 27 inspect / load / apply --human)
# ---------------------------------------------------------------------------


def _yaml_scalar(value: Any) -> str:
    """Format a scalar value for yaml-like output (single line)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    s = str(value)
    if "\n" in s:
        return "|\n" + "\n".join("    " + line for line in s.splitlines())
    return s


def _yaml_like(obj: Any, *, indent: int = 0) -> str:
    """Render ``obj`` as a simple ``key: value`` text dump for humans.

    Supports dicts (one key per line, recursion for nesting), lists
    (one item per line with ``-`` prefix), and scalars. Not a full YAML
    implementation — just enough that 审方药师 can read the dump on a
    TTY without parsing JSON.
    """
    pad = "  " * indent
    out: list[str] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict):
                out.append(f"{pad}{key}:")
                out.append(_yaml_like(value, indent=indent + 1))
            elif isinstance(value, list):
                if not value:
                    out.append(f"{pad}{key}: []")
                else:
                    out.append(f"{pad}{key}:")
                    for item in value:
                        if isinstance(item, (dict, list)):
                            out.append(f"{pad}  -")
                            out.append(_yaml_like(item, indent=indent + 2))
                        else:
                            out.append(f"{pad}  - {_yaml_scalar(item)}")
            else:
                out.append(f"{pad}{key}: {_yaml_scalar(value)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.append(_yaml_like(item, indent=indent + 1))
            else:
                out.append(f"{pad}- {_yaml_scalar(item)}")
    else:
        out.append(f"{pad}{_yaml_scalar(obj)}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _resolve_data_dir(rules_dir: Path, data_dir_arg: str | None) -> Path:
    """Return the absolute data directory.

    默认将 ``data/`` 放在 ``rules/`` 的同级目录(便于 rules/data/audit 三个
    目录保持平级,与院内脚本对"工作区目录布局"的习惯一致)。
    """
    if data_dir_arg:
        return Path(data_dir_arg).resolve()
    return (Path(rules_dir).resolve().parent / "data").resolve()


def _default_index_path(data_dir: Path) -> Path:
    return (data_dir / "rules.json").resolve()


def _load_rules_from_index(index_path: Path) -> list[dict[str, Any]]:
    """Load rules from data/rules.json; raise FileNotFoundError if missing."""
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path}: 索引不存在,请先运行 `pass-fc index-build`"
        )
    payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{index_path}: 索引 JSON 顶层必须是对象")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"{index_path}: rules 字段必须是数组")
    return rules


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_index_build(args: argparse.Namespace) -> int:
    """从 rules/*.md 构建 data/rules.json。

    复用 build-hermes-plugin.py:122-126 main 思路(命令行参数 → collect →
    write),改写为 collect_rx_rules + data/rules.json。原子写:tmp + rename。
    """
    rules_dir = Path(args.rules_dir)
    if not rules_dir.is_dir():
        _emit_error("INVALID_INPUT", f"规则目录不存在: {rules_dir}")
        return 1

    categories_path = Path(args.index)
    try:
        categories = load_categories(categories_path) if categories_path.exists() else None
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1

    try:
        rules = collect_rx_rules(
            rules_dir,
            categories=categories,
            strict_categories=args.strict_categories,
        )
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1

    out_path = args.index_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    payload = {
        "version": _dt.date.today().isoformat(),
        "rules": rules,
    }
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(out_path)
    except OSError as exc:
        _emit_error("INTERNAL_ERROR", f"写入失败: {exc}")
        return 1

    sys.stdout.write(f"ok: 写入 {out_path} (rules={len(rules)})\n")
    return 0


def _render_search_table(
    results: list[dict[str, Any]],
    *,
    use_color: bool = True,
) -> str:
    """Render an aligned text table for the search results.

    task 27: severity 列在 ``use_color=True`` 时按 ANSI 颜色码着色:
      - high → 红
      - medium → 黄
      - low → 绿
    --no-color 全局开关关闭。空结果仍返回 ``(no matches)``。
    """
    headers = ("rule_id", "drug_class", "severity", "evidence_source", "score")
    if not results:
        return "(no matches)\n"

    rows: list[list[str]] = []
    for r in results:
        sev = str(r.get("severity", "")).lower()
        sev_color = _severity_color_code(sev)
        sev_display = str(r.get("severity", "")).upper()
        sev_colored = _colorize(sev_display, sev_color, use_color)
        rows.append([
            str(r.get("rule_id", "")),
            str(r.get("drug_class", "")),
            sev_colored,
            str(r.get("evidence_source", ""))[:32],
            f"{float(r.get('score', 0)):.4f}",
        ])
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]

    def _fmt_row(row: Sequence[str]) -> str:
        return "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers)))

    out = io.StringIO()
    out.write(_fmt_row(headers) + "\n")
    out.write(_fmt_row(["-" * w for w in widths]) + "\n")
    for row in rows:
        out.write(_fmt_row(row) + "\n")
    return out.getvalue()


def _verbose_search_diagnostics(
    query: str,
    rules: list[dict[str, Any]],
    *,
    drug_class: str | None,
    limit: int,
) -> str:
    """Return a verbose-mode diagnostic dump for ``search``.

    task 27 --verbose:额外打印 query_tokens / 命中数 / top-N
    rule_id + score + severity,便于规则维护者调试「为什么这条没
    命中 / 为什么这条排序高」。
    """
    from .runtime import _tokens_zh  # local import to avoid top-level churn
    tokens = sorted(_tokens_zh(query))
    out_lines = [
        "[verbose] query_tokens: " + (" ".join(tokens) if tokens else "(empty)"),
        f"[verbose] drug_class filter: {drug_class or '(none)'}",
        f"[verbose] rules indexed: {len(rules)}",
    ]
    if tokens:
        results = search_rules(query, rules, drug_class=drug_class, limit=limit)
        out_lines.append(f"[verbose] hits: {len(results)}")
        for i, r in enumerate(results, start=1):
            out_lines.append(
                f"[verbose]   #{i:02d}  rule_id={r['rule_id']}  "
                f"severity={r['severity']}  score={float(r['score']):.4f}"
            )
    return "\n".join(out_lines) + "\n"


def cmd_search(args: argparse.Namespace) -> int:
    """按 query 命中 top-N 适用规则;默认表格 + --json 切 JSON。"""
    try:
        rules = _load_rules_from_index(args.index_path)
    except (FileNotFoundError, ValueError) as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1

    drug_class = (args.drug_class or "").strip() or None
    try:
        results = search_rules(args.query, rules, drug_class=drug_class, limit=args.limit)
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1

    use_color = not getattr(args, "no_color", False)
    if getattr(args, "verbose", False):
        sys.stdout.write(
            _verbose_search_diagnostics(
                args.query, rules, drug_class=drug_class, limit=args.limit
            )
        )
    if args.json:
        sys.stdout.write(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        return 0
    sys.stdout.write(_render_search_table(results, use_color=use_color))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """按 rule_id 取规则全文 JSON(默认含 body;--no-body 仅取 summary)。

    task 27 --human:输出 yaml-like 便于人眼查看(默认仍 JSON)。
    """
    try:
        rules = _load_rules_from_index(args.index_path)
    except (FileNotFoundError, ValueError) as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    try:
        result = inspect_rule(args.rule_id, rules, include_body=args.include_body)
    except ValueError as exc:
        _emit_error("RULE_NOT_FOUND", str(exc))
        return 1
    if not result.get("success", False):
        _emit_error("RULE_NOT_FOUND", result.get("error", "unknown"))
        return 1
    if getattr(args, "human", False):
        sys.stdout.write(_yaml_like(result) + "\n")
        return 0
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


def _parse_order_context(raw: str) -> dict[str, Any]:
    """Parse a JSON object string from --order-context; raise ValueError on bad input."""
    if not raw or not raw.strip():
        raise ValueError("--order-context 必填,且为合法 JSON 对象")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--order-context 必须是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--order-context 必须是 JSON 对象(非数组/标量)")
    return parsed


def cmd_load(args: argparse.Namespace) -> int:
    """组装审方草稿(rule + order_context + evidence_excerpt)。

    task 27 --human:输出 yaml-like 便于人眼查看(默认仍 JSON)。
    """
    try:
        rules = _load_rules_from_index(args.index_path)
    except (FileNotFoundError, ValueError) as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    try:
        order_context = _parse_order_context(args.order_context)
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    try:
        result = load_rule(args.rule_id, order_context, rules)
    except ValueError as exc:
        _emit_error("RULE_NOT_FOUND", str(exc))
        return 1
    if not result.get("success", False):
        _emit_error("RULE_NOT_FOUND", result.get("error", "unknown"))
        return 1
    if getattr(args, "human", False):
        sys.stdout.write(_yaml_like(result) + "\n")
        return 0
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """生成审方意见 JSON(confirmed=False 显式)+ audit.jsonl 留痕。

    安全边界:confirmed 强制 False(本工具永不代签),由审方药师在 HIS
    审方栏人工确认后回写 True。

    task 27 --human:输出 yaml-like 便于人眼查看(默认仍 JSON)。
    """
    try:
        rules = _load_rules_from_index(args.index_path)
    except (FileNotFoundError, ValueError) as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    try:
        order_context = _parse_order_context(args.order_context)
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    audit_path = Path(args.audit_path) if args.audit_path else None
    try:
        payload = apply_rule(
            args.rule_id,
            order_context,
            rules,
            operator=args.operator,
            audit_path=audit_path,
        )
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1
    result = {"success": True, "applied": payload}
    if getattr(args, "human", False):
        sys.stdout.write(_yaml_like(result) + "\n")
        return 0
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


# ---------------------------------------------------------------------------
# audit-export (in-CLI helper; task 11 audit.py 将接管本逻辑,CLI 调用接口不变)
# ---------------------------------------------------------------------------


_AUDIT_CSV_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "rule_id",
    "action",
    "operator",
    "session_id",
    "order_hash",
    "confirmed",
)


def _read_audit_events(audit_path: Path) -> list[dict[str, Any]]:
    """Read JSONL audit events; skip blank / malformed lines silently."""
    if not audit_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return out


def cmd_audit_export(args: argparse.Namespace) -> int:
    """月度 audit.jsonl 导出 CSV(utf-8-sig BOM,Excel 可直开)。"""
    audit_path = Path(args.audit_path)
    if not audit_path.exists():
        _emit_error("INVALID_INPUT", f"audit log 不存在: {audit_path}")
        return 1
    month = args.month
    events = [
        ev
        for ev in _read_audit_events(audit_path)
        if str(ev.get("timestamp", "")).startswith(month)
    ]
    if args.out:
        out_path = Path(args.out).resolve()
    else:
        out_path = audit_path.with_name(f"audit-{month}.csv").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_AUDIT_CSV_COLUMNS)
        for ev in events:
            writer.writerow([ev.get(col, "") for col in _AUDIT_CSV_COLUMNS])
    sys.stdout.write(f"ok: 写入 {out_path} (events={len(events)})\n")
    return 0


def _default_archive_dir(audit_path: Path) -> Path:
    """归档目录默认与 audit.jsonl 同目录下的 archive/ 子目录(便于人脑记忆)。"""
    return (audit_path.parent / "archive").resolve()


def cmd_audit_archive(args: argparse.Namespace) -> int:
    """把指定月份的 audit 事件从 audit.jsonl 移走并 gzip 归档到 archive_dir。

    归档后 audit.jsonl 仅保留非当月事件,避免 JSONL 持续膨胀。
    默认拒绝覆盖已存在的归档(--overwrite 可强制)。
    """
    audit_path = Path(args.audit_path)
    if args.archive_dir:
        archive_dir = Path(args.archive_dir).resolve()
    else:
        archive_dir = _default_archive_dir(audit_path)
    try:
        result = _audit_mod.archive_month(
            args.month, audit_path, archive_dir, overwrite=args.overwrite
        )
    except (ValueError, FileNotFoundError) as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 2
    except FileExistsError as exc:
        _emit_error("RULE_DUPLICATE", str(exc))
        return 3
    sys.stdout.write(
        json.dumps(
            {"success": True, "archived": result},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return 0


def cmd_audit_summary(args: argparse.Namespace) -> int:
    """输出当月药事管理简报(markdown 表格,默认 stdout;--json 输出原始聚合)。

    药事办每月需要一页「本月审方情况」:哪几条规则命中最多、哪些药师在用、
    严重度如何分布、有多少审方意见已被人工确认。本子命令把 audit.jsonl 聚合成
    这一页,可直接粘进科务会材料。severity 需 join data/rules.json;索引缺失时
    退化为 unknown 并在简报口径说明中提示。
    """
    audit_path = Path(args.audit_path)
    rules_path: Path | None = None
    if getattr(args, "rules_path", None):
        rules_path = Path(args.rules_path).resolve()
    elif getattr(args, "index_path", None):
        rules_path = Path(args.index_path)
    if rules_path is not None and not rules_path.exists():
        rules_path = None

    try:
        summary = _audit_mod.monthly_summary(
            args.month, audit_path, rules_path, top_n=args.top_n
        )
    except ValueError as exc:
        _emit_error("INVALID_INPUT", str(exc))
        return 1

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return 0

    text = _audit_mod._format_summary_markdown(summary)
    if getattr(args, "out", None):
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        sys.stdout.write(
            f"ok: 写入 {out_path} (events={summary['total_events']})\n"
        )
        return 0
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_audit_rotate(args: argparse.Namespace) -> int:
    """当 audit.jsonl 超过 --max-mb 时,自动归档最老一个月。"""
    audit_path = Path(args.audit_path)
    if args.archive_dir:
        archive_dir = Path(args.archive_dir).resolve()
    else:
        archive_dir = _default_archive_dir(audit_path)
    result = _audit_mod.rotate_if_oversize(
        audit_path, archive_dir, max_mb=float(args.max_mb)
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    # 退出码:实际归档时 0;未触发 rotate 时返回 0(便于 cron 调用);
    # 体积超限但归档已存在需要人工干预时返回 4(便于 shell 告警)。
    if not result.get("rotated") and result.get("error") == "archive_exists":
        return 4
    return 0


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    # 全局参数模板:通过 parents=[_COMMON] 注入到主解析器与每个子解析器,
    # 这样 --rules-dir / --audit-path / --api-host / --api-port / --data-dir
    # 既可以放在子命令之前,也可以放在子命令之后,便于 shell 拼接与 CI 调用。
    _COMMON = argparse.ArgumentParser(add_help=False)
    _COMMON.add_argument(
        "--rules-dir",
        default="./rules",
        help="规则 markdown 目录(默认 ./rules)",
    )
    _COMMON.add_argument(
        "--audit-path",
        default="./audit/audit.jsonl",
        help="audit.jsonl 路径(默认 ./audit/audit.jsonl)",
    )
    _COMMON.add_argument(
        "--api-host",
        default="127.0.0.1",
        help="REST 监听地址(供 api.py 使用,CLI 不直接消费)",
    )
    _COMMON.add_argument(
        "--api-port",
        type=int,
        default=8765,
        help="REST 监听端口(供 api.py 使用,CLI 不直接消费)",
    )
    _COMMON.add_argument(
        "--data-dir",
        default=None,
        help="data 目录(默认根据 --rules-dir 推导为同级 data/)",
    )
    _COMMON.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        help="关闭 severity 颜色码(task 27;默认开启 ANSI 颜色)",
    )
    _COMMON.add_argument(
        "--verbose",
        action="store_true",
        help="search 子命令额外输出 query_tokens / 命中分数 / rule_id 详情(task 27)",
    )

    parser = argparse.ArgumentParser(
        prog="pass-fc",
        parents=[_COMMON],
        description=(
            "用药字段对照工作台 CLI(规则索引 + 检索 + 审方意见 + 月度审计)。"
            "本工具永不代签,所有 apply 输出的 confirmed=False 由审方药师人工确认。"
        ),
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        title="子命令",
        description="可用子命令清单(详见各子命令 --help)",
    )

    # index-build
    p_ib = sub.add_parser(
        "index-build",
        parents=[_COMMON],
        help="从 rules/*.md 构建 data/rules.json 索引",
        description="扫描 --rules-dir 下所有 rx-*.md,解析 frontmatter,生成 data/rules.json。",
    )
    p_ib.add_argument(
        "--index",
        default="./rules_index.json",
        help="rules_index.json 路径(默认 ./rules_index.json)",
    )
    p_ib.add_argument(
        "--out",
        default=None,
        help="输出索引路径(默认根据 --data-dir 推导为 rules.json)",
    )
    p_ib.add_argument(
        "--strict-categories",
        action="store_true",
        help="类别缺失时退出非零(默认仅 warn,便于药事委员会扩类)",
    )

    # search
    p_s = sub.add_parser(
        "search",
        parents=[_COMMON],
        help="按 query 命中 top-N 适用规则",
        description="token-overlap 检索 + severity 二排序。",
    )
    p_s.add_argument("query", help="用药医嘱字段串(中文/英文/混合)")
    p_s.add_argument(
        "--drug-class",
        default=None,
        help="类目过滤(对应 rules_index.json 的 categories)",
    )
    p_s.add_argument(
        "--limit",
        type=int,
        default=5,
        help="最大返回条数(默认 5)",
    )
    p_s.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式(默认文本表格)",
    )

    # inspect
    p_i = sub.add_parser(
        "inspect",
        parents=[_COMMON],
        help="按 rule_id 取规则全文 JSON",
        description="返回 rule summary,默认含 body。",
    )
    p_i.add_argument("--rule-id", required=True, help="规则 slug")
    p_i.add_argument(
        "--include-body",
        dest="include_body",
        action="store_true",
        default=True,
        help="是否返回 body 全文(默认 True)",
    )
    p_i.add_argument(
        "--no-body",
        dest="include_body",
        action="store_false",
        help="不返回 body 全文,仅 summary",
    )
    p_i.add_argument(
        "--human",
        dest="human",
        action="store_true",
        help="输出 yaml-like 便于人眼查看(默认 JSON,task 27)",
    )

    # load
    p_l = sub.add_parser(
        "load",
        parents=[_COMMON],
        help="组装审方草稿(rule + order_context + evidence_excerpt)",
        description="供审方药师人工复核,不写入 audit。",
    )
    p_l.add_argument("--rule-id", required=True)
    p_l.add_argument(
        "--order-context",
        required=True,
        help="处方上下文字典的 JSON 字符串",
    )
    p_l.add_argument(
        "--human",
        dest="human",
        action="store_true",
        help="输出 yaml-like 便于人眼查看(默认 JSON,task 27)",
    )

    # apply
    p_a = sub.add_parser(
        "apply",
        parents=[_COMMON],
        help="生成审方意见 JSON + audit.jsonl 留痕",
        description=(
            "返回 {rule_id, order_hash, session_id, operator, applied_at, "
            "evidence_excerpt, confirmed=False};audit.jsonl 默认追加。"
        ),
    )
    p_a.add_argument("--rule-id", required=True)
    p_a.add_argument("--order-context", required=True)
    p_a.add_argument(
        "--operator",
        required=True,
        help="审方药师 / 临床药师工号(强制必填)",
    )
    p_a.add_argument(
        "--human",
        dest="human",
        action="store_true",
        help="输出 yaml-like 便于人眼查看(默认 JSON,task 27)",
    )

    # audit-export
    p_ae = sub.add_parser(
        "audit-export",
        parents=[_COMMON],
        help="月度 audit.jsonl 导出 CSV(utf-8-sig BOM)",
        description="按 --month 过滤,导出到 --out 或默认 audit-{month}.csv。",
    )
    p_ae.add_argument("--month", required=True, help="YYYY-MM")
    p_ae.add_argument(
        "--out",
        default=None,
        help="输出 CSV 路径(默认 audit-{month}.csv 与 audit.jsonl 同目录)",
    )

    # audit-archive — 把指定月份归档到 gzip JSONL,audit.jsonl 仅留非当月
    p_aa = sub.add_parser(
        "audit-archive",
        parents=[_COMMON],
        help="把指定月份的 audit 事件归档到 gzip JSONL(防 JSONL 膨胀)",
        description=(
            "把 audit.jsonl 中属于 --month 的事件移走并 gzip 压缩到 "
            "archive_dir/audit-{month}.jsonl.gz;归档后 audit.jsonl 仅保留非当月事件。"
        ),
    )
    p_aa.add_argument("--month", required=True, help="YYYY-MM")
    p_aa.add_argument(
        "--archive-dir",
        default=None,
        help="归档目录(默认 audit.jsonl 同级 archive/)",
    )
    p_aa.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的归档文件(默认拒绝覆盖)",
    )

    # audit-summary — 月度药事管理简报(markdown 表格)
    p_as = sub.add_parser(
        "audit-summary",
        parents=[_COMMON],
        help="输出当月药事管理简报(高频规则 / 操作者 / 严重度 / 确认率)",
        description=(
            "聚合 audit.jsonl 当月事件,输出 markdown 简报:高频命中规则 Top-N、"
            "操作者分布、严重度分布(join data/rules.json)、确认率与每日活跃度。"
            "药事办可一键导出粘进月度材料。"
        ),
    )
    p_as.add_argument("--month", required=True, help="YYYY-MM")
    p_as.add_argument(
        "--rules-path",
        default=None,
        help="data/rules.json 路径(默认按 --data-dir 推导;缺失时严重度显示 unknown)",
    )
    p_as.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="高频命中规则截断条数(默认 10)",
    )
    p_as.add_argument(
        "--json",
        action="store_true",
        help="输出原始聚合 JSON(默认 markdown 简报)",
    )
    p_as.add_argument(
        "--out",
        default=None,
        help="把 markdown 简报写入文件(默认输出到 stdout)",
    )

    # audit-rotate — 体积守门:超过阈值自动归档最老一月
    p_ar = sub.add_parser(
        "audit-rotate",
        parents=[_COMMON],
        help="audit.jsonl 超过 --max-mb 时,自动归档最老一个月",
        description=(
            "检查 audit.jsonl 体积,超过 --max-mb 则调 archive_month 归档最老一月。"
            "默认 archive 存在时安全跳过(返回 4 便于 shell 告警)。"
        ),
    )
    p_ar.add_argument(
        "--max-mb",
        type=float,
        default=50.0,
        help="体积上限(MB,默认 50)",
    )
    p_ar.add_argument(
        "--archive-dir",
        default=None,
        help="归档目录(默认 audit.jsonl 同级 archive/)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口;返回退出码(0 = ok,非零 = fail)。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 路径解析:rules-dir → data-dir → index-path
    rules_dir = Path(args.rules_dir)
    data_dir = _resolve_data_dir(rules_dir, args.data_dir)
    args.data_dir = data_dir

    if args.command == "index-build":
        # 写者:支持 --out 自定义输出路径;否则默认 data_dir/rules.json
        if args.out:
            args.index_path = Path(args.out).resolve()
        else:
            args.index_path = _default_index_path(data_dir)
        return cmd_index_build(args)

    # 读者统一用默认 index_path
    args.index_path = _default_index_path(data_dir)

    if args.command == "search":
        return cmd_search(args)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "load":
        return cmd_load(args)
    if args.command == "apply":
        return cmd_apply(args)
    if args.command == "audit-export":
        return cmd_audit_export(args)
    if args.command == "audit-archive":
        return cmd_audit_archive(args)
    if args.command == "audit-summary":
        return cmd_audit_summary(args)
    if args.command == "audit-rotate":
        return cmd_audit_rotate(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())