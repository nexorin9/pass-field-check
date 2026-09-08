# Medication Field Cross-Check Workbench

> Prescription-review pharmacists paste medication order fields (drug / dose / frequency / patient population) into the query box and get matching entries from the hospital's rational-medication rules, evidence excerpts, and severity in seconds — after confirmation, the result is written back to the HIS review column.

A local JSON workbench indexed from the rational-medication rule markdown maintained by the Pharmacy & Therapeutics (P&T) committee: pharmacists hit the top-N applicable rules from the workstation in seconds by field string, each hit is accompanied by its source and evidence excerpt, and only after the pharmacist manually confirms is the result written back to the HIS review column, with the trail folded into the monthly P&T briefing.

## Applicable Scenarios / Target Roles

| Role | When to use | What you get |
|------|-------------|--------------|
| Prescription-review pharmacist | A new order pops up on the HIS review workstation and you need to determine within 30 seconds whether it violates the hospital's rational-medication rules | Matching `rule_id` / `drug_class` / `severity` / evidence excerpt + a JSON snippet pasteable into the HIS review column |
| Clinical pharmacist | Before rounds, batch-review a group of orders (e.g., nephrology / pediatrics / obstetrics) to find entries that violate hospital rules | Top-N rules + filter by `drug_class` + serialized hits for multiple queries, easy for the last check before discharge |
| Pharmacy office / P&T management | Monthly briefings, P&T committee reports, statistics on most-triggered rules, operator distribution, severity distribution | One-click markdown export with top rules, operator distribution, severity distribution, confirmation rate |
| IT department | The HIS vendor needs to integrate the workbench capabilities into the review column fields and needs a stable field contract | REST 5-route API + OpenAPI / ReDoc + JSON Schema, easy contract-based integration |

> The table only describes roles in plain language and the business moment; technical details such as REST paths, JSON Schema fields, and HL7 message structures are unified under "Commands / API / Configuration".

## Capabilities

- **Sub-second matching**: Paste the drug / dose / frequency / patient-population field string into the query box; token overlap scoring plus severity ranking outputs the top-N applicable rules.
- **Evidence visible**: Every hit carries its evidence source (guideline / label / in-hospital protocol excerpt), making it easy for the prescription-review pharmacist to verify and sign off.
- **Chinese-friendly**: Built-in Chinese-English drug name alias fallback plus single-character edit-distance tolerance, covering real-world clinical typos such as "庆大梅素" / "aminoglycosid".
- **HIS contract**: Rule hits are written back to the HIS review column in JSON Schema form (`rule_id` / `order_hash` / `session_id` / `operator` / `evidence_excerpt` / `confirmed`); `confirmed=False` is the default and is advanced manually by the pharmacist.
- **Audit trail**: All `search` / `inspect` / `load` / `apply` actions are appended as JSONL; monthly archiving / rotation / briefing aggregation can be exported with one command.
- **Zero LLM risk**: Rules are maintained manually by the P&T committee; no LLM is introduced to add hallucination or compliance risk.

## Quick Start

```bash
# 1. Install dependencies
cd pass-field-check
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Build the rule index (one-off; rerun after editing rules/*.md)
python scripts/rx-index-build.py
# → generates data/rules.json (with version / rules array)

# 3. Command-line queries
pass-fc search "庆大霉素 儿童 8 岁"
pass-fc inspect --rule-id rx-aminoglycoside-pediatric --include-body
pass-fc apply --rule-id rx-aminoglycoside-pediatric \
              --order-context '{"drug":"庆大霉素","age":8,"egfr":90}' \
              --operator pharmacist-001
# → outputs review-opinion JSON containing order_hash / session_id / confirmed=False

# 4. Start the REST service (for HIS vendor integration / clinical pharmacist calls)
uvicorn pass_field_check.api:app --host 127.0.0.1 --port 8765
# Swagger UI: http://127.0.0.1:8765/docs

# 5. End-to-end verification (one command runs the full chain)
bash scripts/demo.sh
```

## Commands / API / Configuration

### CLI subcommands (`pass-fc`)

| Subcommand | Purpose | Required parameters |
|------------|---------|---------------------|
| `pass-fc index-build` | Rebuild `data/rules.json` from `rules/*.md` | None (default reads `./rules`) |
| `pass-fc search QUERY` | Hit the top-N rules by field string | `QUERY`; optional `--drug-class`, `--limit`, `--json` |
| `pass-fc inspect` | View the full text of a rule | `--rule-id`, `[--include-body]` |
| `pass-fc load` | Load a rule plus order context, generate evidence_excerpt | `--rule-id`, `--order-context` |
| `pass-fc apply` | Generate review-opinion JSON (`confirmed=False`) | `--rule-id`, `--order-context`, `--operator` |
| `pass-fc audit-export` | Export audit.jsonl to CSV by month | `--month YYYY-MM` |
| `pass-fc audit-archive` | Archive a given month to gzip | `--month YYYY-MM` |
| `pass-fc audit-rotate` | Auto-archive the oldest month when over 50MB | None |
| `pass-fc audit-summary` | Output the monthly statistics as a markdown briefing | `--month YYYY-MM`; optional `--top-n`, `--json`, `--out` |

### REST routes (FastAPI, default 127.0.0.1:8765)

| Method | Path | Key request fields | Key response fields |
|--------|------|---------------------|---------------------|
| POST | `/search` | `query` / `drug_class` / `limit` | `rule_id` / `drug_class` / `severity` / `score` / `evidence_excerpt` |
| POST | `/inspect` | `rule_id` / `include_body` | Full rule text + `file_path` |
| POST | `/load` | `rule_id` / `order_context` | Rule + evidence_excerpt + order_context |
| POST | `/apply` | `rule_id` / `order_context` / `operator` | `order_hash` / `session_id` / `confirmed` / `applied_at` |
| POST | `/audit/export` | `month` | CSV file stream (utf-8-sig BOM) |
| GET | `/audit/events` | query: `operator` / `rule_id` / `start_date` / `end_date` / `page` / `page_size` | `{events, total, page, page_size}` |
| GET | `/docs` | — | Swagger UI (HTML) |
| GET | `/redoc` | — | ReDoc (HTML) |
| GET | `/openapi.json` | — | OpenAPI 3.1 schema |

### `.env.example` configuration

| Config | Default | Description |
|--------|---------|-------------|
| `RX_RULES_DIR` | `./rules` | Rules markdown directory |
| `RX_INDEX_PATH` | `./data/rules.json` | Index output path |
| `RX_AUDIT_PATH` | `./audit/audit.jsonl` | Audit log JSONL path |
| `RX_API_HOST` | `127.0.0.1` | FastAPI bind address |
| `RX_API_PORT` | `8765` | FastAPI bind port |

> Note: The HIS vendor field JSON Schema and mock stubs are **for local verification only** and are not the final integration surface; the formal integration must follow the in-hospital interface spec `SCHEMA_HIS_REVIEW_FIELD` in `contract.py`.

## Typical Scenarios

### Scenario 1: 30-second evidence lookup at the review workstation

1. Trigger: The HIS review workstation surfaces a new order "Gentamicin iv 80mg qd, 8 years old".
2. Action: The pharmacist pastes the field string into `pass-fc search "庆大霉素 iv 80mg 8 岁" --limit 5`.
3. Output: The top-5 hit is `rx-aminoglycoside-pediatric` (severity=high, evidence: Gentamicin should be avoided in children under 8; if necessary, monitor hearing and renal function).
4. Closure: Pharmacist manual verification → `pass-fc apply --rule-id rx-aminoglycoside-pediatric --order-context '{...}' --operator pharmacist-001` → paste the returned JSON (containing `confirmed=False`, evidence hash, `session_id`) into the HIS review column.

### Scenario 2: Clinical pharmacist batch-check before rounds

1. Trigger: Morning hand-off in nephrology — the clinical pharmacist needs to quickly find entries with metformin / ACEI / quinolone renal-injury or pregnancy-contraindication concerns in this group's orders.
2. Action: A batch script loops through `pass-fc search "二甲双胍 egfr=45"` / `"卡托普利 孕妇"` / `"环丙沙星 16 岁"`, filtering by `drug_class` and outputting the top-3.
3. Output: The hits for the three queries are consolidated into a markdown table, attached to the rounds section for the attending physician's review.

### Scenario 3: Monthly P&T briefing

1. Trigger: Before the 5th of each month, the pharmacy office needs to compile last month's review records into a briefing for the P&T committee.
2. Action: `pass-fc audit-export --month 2026-08` (opens directly in Excel) + `pass-fc audit-summary --month 2026-08` (markdown briefing).
3. Output: The CSV contains `operator` / `rule_id` / `order_hash` / `confirmed` for every review record; the markdown briefing contains the top-10 triggered rules, operator distribution, severity distribution, and confirmation rate.

## Output Samples

### `pass-fc search "庆大霉素 儿童 8 岁"`

```
rule_id                          drug_class    severity  evidence_source               score
-------------------------------  ------------  --------  ----------------------------  ------
rx-aminoglycoside-pediatric      抗菌药        high      院内用药手册 第 4.2 节          0.84
rx-aminoglycoside-tdm            抗菌药        medium    治疗药物监测指南 TDM-09       0.62
rx-quinolone-pediatric           抗菌药        high      院内用药手册 第 4.5 节          0.41
```

### `pass-fc apply --rule-id rx-aminoglycoside-pediatric --order-context '{"drug":"庆大霉素","age":8,"egfr":90}' --operator pharmacist-001`

```json
{
  "rule_id": "rx-aminoglycoside-pediatric",
  "drug_class": "抗菌药",
  "severity": "high",
  "evidence_source": "院内用药手册 第 4.2 节",
  "evidence_excerpt": "庆大霉素属氨基糖苷类，8 岁以下儿童应避免使用；确有指征时需监测听力与肾功能。",
  "order_context": {"drug": "庆大霉素", "age": 8, "egfr": 90},
  "order_hash": "3f2b9c1a8e7d...",
  "session_id": "9c2e4f1b7a3d5c8e",
  "applied_at": "2026-09-08T16:48:12+08:00",
  "operator": "pharmacist-001",
  "confirmed": false
}
```

### `pass-fc audit-export --month 2026-08` (CSV header)

```
timestamp,rule_id,order_hash,session_id,operator,action,confirmed
2026-08-30T09:15:23+08:00,rx-aminoglycoside-pediatric,3f2b...,9c2e...,pharmacist-001,apply,false
2026-08-30T09:42:11+08:00,rx-nsaid-pregnancy,71ad...,3e1b...,pharmacist-002,apply,false
...
```

> The CSV contains a BOM (utf-8-sig) so that double-clicking it in Excel opens it directly without garbled text.

### `pass-fc audit-summary --month 2026-08` (monthly briefing excerpt)

```markdown
## Overview

- Total events: **8**
- Review opinions generated (apply): **8**; pharmacist-confirmed write-back: **0**, confirmation rate **0.0%**
- Involving **5** rules and **3** operators

## High-frequency Triggered Rules

| Rank | Rule | Severity | Hits | Share |
|------|------|----------|------|-------|
| 1 | rx-aminoglycoside-pediatric | high | 3 | 37.5% |
| 2 | rx-nsaid-pregnancy | high | 2 | 25.0% |

## Daily Activity

| Date | Events | Distribution |
|------|--------|--------------|
| 2026-08-30 | 8 | ████████████████████████ |
```

> The briefing also includes operator distribution, severity distribution, and a calibration note; `--out brief.md` writes it to disk ready to paste into the department meeting materials, and `--json` outputs the raw aggregation for BI secondary statistics.

## Architecture and Data Flow

```
rules/*.md          rules_index.json
    │                    │
    ▼                    ▼
scripts/rx-index-build.py
    │
    ▼
data/rules.json  ◀──────────┐
    │                       │
    ▼                       │
pass_field_check/           │
  ├─ index_builder.py       │  (frontmatter parsing + dedup + category validation)
  ├─ runtime.py             │  (token overlap scoring + severity ranking + fuzzy)
  ├─ tools.py               │  (4-tool schema + apply_rule review opinion)
  ├─ audit.py               │  (JSONL append + monthly archive + summary)
  └─ contract.py            │  (HIS field JSON Schema)
    │                       │
    ▼                       │
cli.py   ──►  pass-fc entry  │
api.py   ──►  FastAPI 8765  │
    │                       │
    └─────────► audit/audit.jsonl
                    │
                    ▼
            pass-fc audit-export / audit-summary
```

Main path: `rules/*.md` → `rx-index-build` → `data/rules.json` → `runtime.search_rules` → 4 tools (`search` / `inspect` / `load` / `apply`) → `audit.jsonl` → monthly export / briefing.

## Safety and Compliance Boundaries

- **No diagnostic or treatment recommendations**: This workbench only presents factual "rule match + evidence excerpt + severity"; all `confirmed` fields default to `false` and are advanced manually by the prescription-review / clinical pharmacist after confirmation.
- **No automatic write-back to the HIS review column**: `apply_rule` only generates a review-opinion JSON carrying `order_hash` / `session_id`; whether it is written back is decided by the HIS workstation, and mock stubs are for local testing only.
- **Auditable**: All `search` / `inspect` / `load` / `apply` actions leave a trail with ISO 8601 timestamps and `operator`; monthly archiving / rotation prevents JSONL bloat.

## Project Structure

```
pass-field-check/
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
├── qrcode.jpg
├── pass_field_check/
│   ├── __init__.py
│   ├── index_builder.py        # parse_rx_rule / collect_rx_rules / slugify
│   ├── runtime.py              # _tokens_zh / _score_rx / search_rules / extract_evidence_excerpt
│   ├── tools.py                # 4-tool schema + register_rx_tools
│   ├── audit.py                # append_audit / export_month / query_events / monthly_summary
│   ├── contract.py             # HIS review-column field JSON Schema
│   ├── api.py                  # FastAPI 5 routes
│   └── cli.py                  # pass-fc entry + 8 subcommands
├── rules/
│   └── rx-*.md                 # Rational-medication rules maintained by the P&T committee (≥12 samples)
├── scripts/
│   ├── rx-index-build.py       # Build data/rules.json
│   ├── check-rx-rule-workbench.py  # 4-tool contract + search→inspect smoke test
│   └── demo.sh                 # One-line full-chain verification
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── mock_prescriptions.json
│   │   └── mock_his_fields.json
│   ├── test_index.py
│   ├── test_tokens.py
│   ├── test_search.py
│   ├── test_tools.py
│   ├── test_cli.py
│   ├── test_api.py
│   ├── test_audit.py
│   ├── test_fixtures.py
│   ├── test_perf.py
│   └── test_e2e.py
├── data/                       # Build artifacts (runtime-generated, gitignored)
├── audit/                      # JSONL audit (runtime-generated, gitignored)
├── logs/                       # Runtime logs (runtime-generated, gitignored)
└── docs/screenshots/
```

## License

MIT

---

## Follow Us

Scan the QR code to follow our official account for updates and community access:

![Follow Us](qrcode.jpg)