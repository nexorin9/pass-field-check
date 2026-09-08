from pathlib import Path

import pytest

from pass_field_check.index_builder import (
    collect_rx_rules,
    load_categories,
    parse_rx_rule,
    slugify,
)


def test_parse_rx_rule_happy(tmp_path: Path):
    path = tmp_path / "rx-example.md"
    path.write_text(
        """---
rule_id: rx-example
drug_class: 儿科
severity: high
evidence_source: 院内规程
population: 儿童
applies_to:
  age_max: 12
  pregnancy: any
---
# 示例规则
儿童使用前应复核剂量。

## 依据片段
证据内容。
""",
        encoding="utf-8",
    )

    record = parse_rx_rule(path)

    assert record["rule_id"] == "rx-example"
    assert record["drug_class"] == "儿科"
    assert record["severity"] == "high"
    assert record["applies_to"] == ["age_max=12", "pregnancy=any"]
    assert "证据内容" in record["body"]


def test_parse_rx_rule_missing_rule_id(tmp_path: Path):
    path = tmp_path / "rx-example.md"
    path.write_text(
        """---
drug_class: 儿科
severity: medium
evidence_source: 院内规程
population: 儿童
---
正文
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=str(path)):
        parse_rx_rule(path)


def test_parse_rx_rule_bad_severity(tmp_path: Path):
    path = tmp_path / "rx-example.md"
    path.write_text(
        """---
rule_id: rx-example
drug_class: 儿科
severity: 严重
evidence_source: 院内规程
population: 儿童
---
正文
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="严重"):
        parse_rx_rule(path)


def test_parse_rx_rule_yaml_light_compat(tmp_path: Path):
    path = tmp_path / "rx-example.md"
    path.write_text(
        """rule_id: rx-example
drug_class: 儿科
severity: low
evidence_source: 院内规程
population: 儿童
body: |
  这是一个多行依据片段
  第二行也保留
# 标题
实际正文
""",
        encoding="utf-8",
    )

    record = parse_rx_rule(path)

    assert record["rule_id"] == "rx-example"
    assert "这是一个多行依据片段" in record["body"]
    assert "实际正文" in record["body"]


# ---------------------------------------------------------------------------
# Task 4: collect_rx_rules + slugify + load_categories
# ---------------------------------------------------------------------------


def _write_rule(
    directory: Path,
    *,
    rule_id: str,
    drug_class: str = "儿科",
    severity: str = "high",
    body_heading: str = "正文",
    extra_body: str = "临床要点。",
) -> Path:
    """Helper: write a single rx-*.md rule into ``directory`` and return its path."""
    path = directory / f"{rule_id}.md"
    path.write_text(
        f"""---
rule_id: {rule_id}
drug_class: {drug_class}
severity: {severity}
evidence_source: 院内规程
population: 儿童
---
# {body_heading}
{extra_body}
""",
        encoding="utf-8",
    )
    return path


def test_slugify_kebab_case():
    assert slugify("rx-aminoglycoside-pediatric") == "rx-aminoglycoside-pediatric"
    assert slugify("RX Aminoglycoside Pediatric") == "rx-aminoglycoside-pediatric"
    assert slugify("用药字段对照") == ""
    assert slugify("Drug Class 001") == "drug-class-001"
    assert slugify(None) == ""  # type: ignore[arg-type]
    assert slugify("---") == ""
    assert slugify("a/b\\c") == "a-b-c"


def test_collect_rx_rules_count_real_rules_dir():
    """The committed rules/ directory should always parse >=12 unique rules."""
    rules_dir = Path(__file__).resolve().parents[1] / "rules"
    records = collect_rx_rules(rules_dir)
    assert len(records) >= 12, f"expected >=12 rules, got {len(records)}"
    # rule_id must be unique and stable-sorted
    rule_ids = [record["rule_id"] for record in records]
    assert rule_ids == sorted(rule_ids)
    assert len(set(rule_ids)) == len(rule_ids)
    # Each record must carry the absolute file_path for audit
    for record in records:
        assert Path(record["file_path"]).is_file()


def test_collect_rx_rules_dedup_raises(tmp_path: Path):
    directory = tmp_path / "rules"
    directory.mkdir()
    _write_rule(directory, rule_id="rx-dup")
    # 第二条规则使用不同的文件名以避免被前一条覆盖,但 rule_id 仍为 rx-dup
    (directory / "rx-dup-extra.md").write_text(
        """---
rule_id: rx-dup
drug_class: 儿科
severity: high
evidence_source: 院内规程
population: 儿童
---
第二条
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rule_id duplicate"):
        collect_rx_rules(directory)


def test_collect_rx_rules_bad_severity_in_one_file(tmp_path: Path):
    directory = tmp_path / "rules"
    directory.mkdir()
    _write_rule(directory, rule_id="rx-good", severity="high")
    bad = directory / "rx-bad-sev.md"
    bad.write_text(
        """---
rule_id: rx-bad-sev
drug_class: 儿科
severity: 严重
evidence_source: 院内规程
population: 儿童
---
正文
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rx-bad-sev"):
        collect_rx_rules(directory)


def test_collect_rx_rules_missing_rules_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="规则目录不存在"):
        collect_rx_rules(tmp_path / "no-such-dir")


def test_collect_rx_rules_skip_non_rx_markdown(tmp_path: Path):
    """Files like ``audit-*.md`` or ``readme.md`` must be ignored."""
    directory = tmp_path / "rules"
    directory.mkdir()
    _write_rule(directory, rule_id="rx-real")
    (directory / "audit-2026-09.md").write_text("# 审计\n无关", encoding="utf-8")
    (directory / "readme.md").write_text("# README\n无关", encoding="utf-8")
    # rx-empty.md 也参与 rglob 匹配 rx-*.md,但因为没有 frontmatter,
    # collect_rx_rules 应该让其走 parse_rx_rule 抛错以暴露数据质量问题。
    (directory / "rx-empty.md").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="缺少 frontmatter"):
        collect_rx_rules(directory)

    # 删除空的 rx-empty.md 后,只保留唯一合法规则
    (directory / "rx-empty.md").unlink()
    records = collect_rx_rules(directory)
    rule_ids = [record["rule_id"] for record in records]
    assert rule_ids == ["rx-real"]


def test_collect_rx_rules_category_warn_default(tmp_path: Path):
    directory = tmp_path / "rules"
    directory.mkdir()
    _write_rule(directory, rule_id="rx-known", drug_class="儿科")
    _write_rule(directory, rule_id="rx-newcat", drug_class="罕见病")

    with pytest.warns(UserWarning, match="罕见病"):
        collect_rx_rules(directory, categories=["儿科", "心血管"])


def test_collect_rx_rules_category_strict_raises(tmp_path: Path):
    directory = tmp_path / "rules"
    directory.mkdir()
    _write_rule(directory, rule_id="rx-known", drug_class="儿科")
    _write_rule(directory, rule_id="rx-newcat", drug_class="罕见病")

    with pytest.raises(ValueError, match="罕见病"):
        collect_rx_rules(directory, categories=["儿科", "心血管"], strict_categories=True)


def test_load_categories_normal(tmp_path: Path):
    index = tmp_path / "rules_index.json"
    index.write_text(
        '{"version": "2026.09.08", "categories": ["抗菌药", "儿科"]}',
        encoding="utf-8",
    )
    assert load_categories(index) == ["抗菌药", "儿科"]


def test_load_categories_missing_field(tmp_path: Path):
    index = tmp_path / "rules_index.json"
    index.write_text('{"version": "2026.09.08"}', encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 categories 字段"):
        load_categories(index)


def test_load_categories_missing_file(tmp_path: Path):
    with pytest.raises(ValueError, match="rules_index.json 不存在"):
        load_categories(tmp_path / "no.json")


# ---------------------------------------------------------------------------
# Task 17: rules_index.json 类别驱动 + --strict-categories CLI subprocess 验证
# ---------------------------------------------------------------------------


def test_rx_index_build_strict(tmp_path: Path):
    """``rx-index-build.py --strict-categories`` 在 drug_class 不在 categories 时必须退出非零。

    设计取舍:默认仅 warn 而不阻塞(药事办扩展类别是常见运营动作),但
    ``--strict-categories`` 给出严格守门入口 -- 让 CI / 上线前体检能在新增类别
    未纳入 rules_index.json 时阻止发布。
    """
    import subprocess
    import sys

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    # 规则 drug_class='神外科' 不在 rules_index.json categories 内
    _write_rule(rules_dir, rule_id="rx-neuro", drug_class="神外科", severity="medium")

    index_path = tmp_path / "rules_index.json"
    index_path.write_text(
        '{"version": "2026.09.08", "categories": ["抗菌药", "儿科"]}',
        encoding="utf-8",
    )

    out_path = tmp_path / "rules.json"
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "rx-index-build.py"
    )

    # 默认 (无 --strict-categories):仅 warn,不阻塞,exit 0
    result_default = subprocess.run(
        [
            sys.executable,
            str(script),
            "--rules-dir",
            str(rules_dir),
            "--out",
            str(out_path),
            "--index",
            str(index_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result_default.returncode == 0, (
        f"默认应仅 warn 不阻塞。stdout={result_default.stdout!r} "
        f"stderr={result_default.stderr!r}"
    )
    assert "ok:" in result_default.stdout
    assert out_path.is_file()

    # 清理 out_path,验证 --strict-categories 行为
    out_path.unlink()

    # --strict-categories:drug_class 不在 categories → 退出非零
    result_strict = subprocess.run(
        [
            sys.executable,
            str(script),
            "--rules-dir",
            str(rules_dir),
            "--out",
            str(out_path),
            "--index",
            str(index_path),
            "--strict-categories",
        ],
        capture_output=True,
        text=True,
    )
    assert result_strict.returncode != 0, (
        f"--strict-categories 应拒绝未知 drug_class。stdout={result_strict.stdout!r} "
        f"stderr={result_strict.stderr!r}"
    )
    # stderr 应包含 drug_class=神外科 的提示,便于 CI / shell 捕获失败原因
    assert "神外科" in result_strict.stderr or "rx-neuro" in result_strict.stderr
    # 严格模式下不应写出 rules.json(避免半成品)
    assert not out_path.exists(), "严格模式下不应写出 rules.json"


# ---------------------------------------------------------------------------
# Task 16: parse_rx_rule 边界用例(BOM / 嵌套 / 多行 / 未知字段 / 缺 body)
# ---------------------------------------------------------------------------


def test_parse_rx_rule_bom(tmp_path: Path):
    """UTF-8 BOM 文件必须被正确剥离,rule_id 与 severity 解析正确。"""
    path = tmp_path / "rx-bom.md"
    payload = (
        "---\n"
        "rule_id: rx-bom-test\n"
        "drug_class: 儿科\n"
        "severity: medium\n"
        "evidence_source: 院内规程\n"
        "population: 儿童\n"
        "---\n"
        "# 标题\n"
        "正文内容\n"
    )
    # 直接以 utf-8 写出含 BOM 的字节,模拟 Windows / 部分编辑器输出。
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    record = parse_rx_rule(path)
    assert record["rule_id"] == "rx-bom-test"
    assert record["severity"] == "medium"
    assert "正文内容" in record["body"]


def test_parse_rx_rule_nested_applies_to(tmp_path: Path):
    """applies_to 同时支持 list 与 dict 形态,统一归一为 list[str]。"""
    path_list = tmp_path / "rx-list.md"
    path_list.write_text(
        """---
rule_id: rx-list-form
drug_class: 儿科
severity: high
evidence_source: 院内规程
population: 儿童
applies_to:
  - age_max=12
  - pregnancy=any
---
# 标题
正文
""",
        encoding="utf-8",
    )
    record_list = parse_rx_rule(path_list)
    assert record_list["applies_to"] == ["age_max=12", "pregnancy=any"]

    path_dict = tmp_path / "rx-dict.md"
    path_dict.write_text(
        """---
rule_id: rx-dict-form
drug_class: 儿科
severity: high
evidence_source: 院内规程
population: 儿童
applies_to:
  age_max: 12
  pregnancy: any
---
# 标题
正文
""",
        encoding="utf-8",
    )
    record_dict = parse_rx_rule(path_dict)
    assert record_dict["applies_to"] == ["age_max=12", "pregnancy=any"]


def test_parse_rx_rule_unknown_field(tmp_path: Path):
    """未知字段必须被保留在 extras 中而不抛错,便于 forward-compat。"""
    path = tmp_path / "rx-unknown.md"
    path.write_text(
        """---
rule_id: rx-unknown-field
drug_class: 儿科
severity: low
evidence_source: 院内规程
population: 儿童
applies_to: [age_max=12]
custom_owner: 临床药学组
revision_note: 2026-09 修订
internal_code: 99-A
---
# 标题
正文内容
""",
        encoding="utf-8",
    )

    record = parse_rx_rule(path)
    assert record["rule_id"] == "rx-unknown-field"
    # 已知字段不进入 extras
    assert "rule_id" not in record.get("extras", {})
    assert "drug_class" not in record.get("extras", {})
    # 未知标量字段被保留
    assert record["extras"]["custom_owner"] == "临床药学组"
    assert record["extras"]["revision_note"] == "2026-09 修订"
    assert record["extras"]["internal_code"] == "99-A"


def test_parse_rx_rule_multiline_value(tmp_path: Path):
    """frontmatter 中 ``|`` 多行 value 必须被完整保留到 body。"""
    path = tmp_path / "rx-multiline.md"
    path.write_text(
        """---
rule_id: rx-multiline-body
drug_class: 儿科
severity: medium
evidence_source: 院内规程
population: 儿童
body: |
  第一行依据片段
  第二行依据片段
  第三行依据片段
---
# 标题
""",
        encoding="utf-8",
    )

    record = parse_rx_rule(path)
    assert "第一行依据片段" in record["body"]
    assert "第二行依据片段" in record["body"]
    assert "第三行依据片段" in record["body"]


def test_parse_rx_rule_empty_body(tmp_path: Path):
    """body 缺失或仅含空白必须 raise,避免静默通过审方。"""
    path = tmp_path / "rx-empty.md"
    path.write_text(
        """---
rule_id: rx-empty-body
drug_class: 儿科
severity: high
evidence_source: 院内规程
population: 儿童
---
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="body 缺失或为空"):
        parse_rx_rule(path)


def test_parse_rx_rule_bad_severity_zh(tmp_path: Path):
    """中文 severity 写法必须被拒绝,防止药事办误用。"""
    for idx, bad_severity in enumerate(["高", "严重", "High", "MEDIUM"]):
        # 使用 kebab-case rule_id 以绕过 rule_id 校验,让测试聚焦 severity 拦截。
        path = tmp_path / f"rx-bad-sev-{idx}.md"
        path.write_text(
            f"""---
rule_id: rx-bad-sev-{idx}
drug_class: 儿科
severity: {bad_severity}
evidence_source: 院内规程
population: 儿童
---
# 标题
正文
""",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="severity 必须是 high/medium/low"):
            parse_rx_rule(path)
