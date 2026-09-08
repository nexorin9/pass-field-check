# 用药字段对照工作台

> 审方药师把用药医嘱字段（药名 / 剂量 / 频次 / 人群）粘进查询框，秒级拿到本院合理用药规则里命中条目、依据片段与严重度——确认后再写回 HIS 审方栏。

把药事委员会维护的合理用药规则 markdown 一处建索引成本地 JSON 工作台：审方药师在工作站按字段串秒级命中 top-N 适用规则，每条命中都附出处与依据片段，药师人工确认后才写回 HIS 审方栏，留痕进入药事管理简报。

## 适用场景 / 目标岗位

| 岗位 | 什么时候用 | 得到什么 |
|------|------------|----------|
| 审方药师 | HIS 审方工作站看到一张新处方，需要 30 秒内判断是否触犯本院合理用药规则 | 命中规则的 rule_id / drug_class / severity / 依据片段 + 可粘贴进 HIS 审方栏的字段 JSON |
| 临床药师 | 查房前需要批量复核一组医嘱（如肾内科 / 儿科 / 产科），找出违背本院规则的条目 | top-N 规则 + 按 drug_class 过滤 + 多 query 串行命中，便于出院前最后一道核对 |
| 药事办 / 药事管理 | 月度简报、药事委员会汇报，需要统计当月被触发最多的规则、操作者分布与严重度分布 | 一键导出的 markdown 简报，含 top 规则、操作者分布、严重度分布、确认率 |
| 信息科 | HIS 厂家需要把工作台能力接入审方栏字段，需要一份稳定的字段契约面 | REST 5 路由 + OpenAPI / ReDoc + JSON Schema，便于按合同对接 |

> 表格只写岗位口语与业务时刻；REST 路径、JSON Schema 字段、HL7 消息结构等技术细节统一放在「命令 / API / 配置说明」。

## 能力要点

- **秒级命中**：药名 / 剂量 / 频次 / 人群字段串直接粘进查询框，token 重叠打分 + 严重度排序输出 top-N 适用规则。
- **依据可见**：每条命中都带证据出处（指南 / 说明书 / 院内规程片段），便于审方药师复核与签字留痕。
- **中文友好**：内置中英文药名别名兜底与单字编辑距离容错，覆盖「庆大梅素」「aminoglycosid」等临床真实错字。
- **HIS 契约**：规则命中以 JSON Schema 形式（rule_id / order_hash / session_id / operator / evidence_excerpt / confirmed）回写到 HIS 审方栏，confirmed=False 默认值由药师人工推进。
- **审计留痕**：所有 search / inspect / load / apply 行为以 JSONL 追加，月度归档 / 旋转 / 简报聚合可一键导出。
- **零 LLM 风险**：规则由药事委员会人工维护，不引入 LLM 增加幻觉或合规风险。

## 快速开始

```bash
# 1. 安装依赖
cd pass-field-check
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 构建规则索引（一次即可，后续修改 rules/*.md 重跑）
python scripts/rx-index-build.py
# → 生成 data/rules.json（含 version / rules 数组）

# 3. 命令行查询
pass-fc search "庆大霉素 儿童 8 岁"
pass-fc inspect --rule-id rx-aminoglycoside-pediatric --include-body
pass-fc apply --rule-id rx-aminoglycoside-pediatric \
              --order-context '{"drug":"庆大霉素","age":8,"egfr":90}' \
              --operator pharmacist-001
# → 输出含 order_hash / session_id / confirmed=False 的审方意见 JSON

# 4. 启动 REST（供 HIS 厂家对接 / 临床药师调用）
uvicorn pass_field_check.api:app --host 127.0.0.1 --port 8765
# Swagger UI: http://127.0.0.1:8765/docs

# 5. 端到端验证（一条命令跑完全链路）
bash scripts/demo.sh
```

## 命令 / API / 配置说明

### CLI 子命令（`pass-fc`）

| 子命令 | 用途 | 必填参数 |
|--------|------|----------|
| `pass-fc index-build` | 从 `rules/*.md` 重建 `data/rules.json` | 无（默认读 `./rules`） |
| `pass-fc search QUERY` | 按字段串命中 top-N 规则 | `QUERY`；可选 `--drug-class`、`--limit`、`--json` |
| `pass-fc inspect` | 查看某条规则全文 | `--rule-id`、`[--include-body]` |
| `pass-fc load` | 加载规则 + 医嘱上下文，生成 evidence_excerpt | `--rule-id`、`--order-context` |
| `pass-fc apply` | 生成审方意见 JSON（confirmed=False） | `--rule-id`、`--order-context`、`--operator` |
| `pass-fc audit-export` | 按月导出 audit.jsonl 到 CSV | `--month YYYY-MM` |
| `pass-fc audit-archive` | 归档指定月份到 gzip | `--month YYYY-MM` |
| `pass-fc audit-rotate` | 超过 50MB 自动归档最老月份 | 无 |
| `pass-fc audit-summary` | 输出月度统计 markdown 简报 | `--month YYYY-MM`；可选 `--top-n`、`--json`、`--out` |

### REST 路由（FastAPI，默认 127.0.0.1:8765）

| 方法 | 路径 | 请求体关键字段 | 响应关键字段 |
|------|------|----------------|--------------|
| POST | `/search` | `query` / `drug_class` / `limit` | `rule_id` / `drug_class` / `severity` / `score` / `evidence_excerpt` |
| POST | `/inspect` | `rule_id` / `include_body` | 规则全文 + `file_path` |
| POST | `/load` | `rule_id` / `order_context` | 规则 + evidence_excerpt + order_context |
| POST | `/apply` | `rule_id` / `order_context` / `operator` | `order_hash` / `session_id` / `confirmed` / `applied_at` |
| POST | `/audit/export` | `month` | CSV 文件流（utf-8-sig BOM） |
| GET | `/audit/events` | query: `operator` / `rule_id` / `start_date` / `end_date` / `page` / `page_size` | `{events, total, page, page_size}` |
| GET | `/docs` | — | Swagger UI（HTML） |
| GET | `/redoc` | — | ReDoc（HTML） |
| GET | `/openapi.json` | — | OpenAPI 3.1 schema |

### `.env.example` 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RX_RULES_DIR` | `./rules` | 规则 markdown 目录 |
| `RX_INDEX_PATH` | `./data/rules.json` | 索引输出路径 |
| `RX_AUDIT_PATH` | `./audit/audit.jsonl` | 审计日志 JSONL 路径 |
| `RX_API_HOST` | `127.0.0.1` | FastAPI 监听地址 |
| `RX_API_PORT` | `8765` | FastAPI 监听端口 |

> 注：HIS 厂家对接的字段 JSON Schema 与 mock 替身**仅用于本地验证**，不作为最终 integration surface；正式接入须按 `contract.py` 的 `SCHEMA_HIS_REVIEW_FIELD` 走院内接口规范。

## 典型场景

### 场景 1：审方工作站 30 秒找依据

1. 触发：HIS 审方工作站弹出「庆大霉素 iv 80mg qd 8 岁」新医嘱。
2. 操作：审方药师把字段串粘进 `pass-fc search "庆大霉素 iv 80mg 8 岁" --limit 5`。
3. 产出：top-5 命中 `rx-aminoglycoside-pediatric`（severity=high，evidence：庆大霉素 8 岁儿童应避免，必要时监测听力与肾功能）。
4. 闭环：药师人工复核 → `pass-fc apply --rule-id rx-aminoglycoside-pediatric --order-context '{...}' --operator pharmacist-001` → 把返回的 JSON（含 `confirmed=False`、证据 hash、session_id）粘进 HIS 审方栏。

### 场景 2：临床药师查房前批量核

1. 触发：肾内科早交班，临床药师需要快速找出本组医嘱里二甲双胍 / ACEI / 喹诺酮的肾损 / 孕期禁忌条目。
2. 操作：批量脚本循环 `pass-fc search "二甲双胍 egfr=45"` / `"卡托普利 孕妇"` / `"环丙沙星 16 岁"`，输出按 drug_class 过滤后的 top-3。
3. 产出：3 个 query 的命中结果统一存为 markdown 表格，附在查房小节里给主治医师过目。

### 场景 3：药事办月度简报

1. 触发：每月 5 号前药事办需要把上月审方记录做成简报给药事委员会。
2. 操作：`pass-fc audit-export --month 2026-08`（Excel 直开）+ `pass-fc audit-summary --month 2026-08`（markdown 简报）。
3. 产出：CSV 含每条审方记录的 operator / rule_id / order_hash / confirmed；markdown 简报含 top-10 触发规则、操作者分布、严重度分布、确认率。

## 输出样例

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

### `pass-fc audit-export --month 2026-08`（CSV 列头）

```
timestamp,rule_id,order_hash,session_id,operator,action,confirmed
2026-08-30T09:15:23+08:00,rx-aminoglycoside-pediatric,3f2b...,9c2e...,pharmacist-001,apply,false
2026-08-30T09:42:11+08:00,rx-nsaid-pregnancy,71ad...,3e1b...,pharmacist-002,apply,false
...
```

> CSV 含 BOM（utf-8-sig），Excel 双击可直接打开不乱码。

### `pass-fc audit-summary --month 2026-08`（月度简报节选）

```markdown
## 概览

- 事件总数:**8** 条
- 生成审方意见(apply):**8** 条;药师已回写确认 **0** 条,确认率 **0.0%**
- 涉及规则 **5** 条,参与人员 **3** 人

## 高频命中规则

| 排名 | 规则 | 严重度 | 命中次数 | 占比 |
|------|------|--------|----------|------|
| 1 | rx-aminoglycoside-pediatric | high | 3 | 37.5% |
| 2 | rx-nsaid-pregnancy | high | 2 | 25.0% |

## 每日活跃度

| 日期 | 事件数 | 分布 |
|------|--------|------|
| 2026-08-30 | 8 | ████████████████████████ |
```

> 简报还含操作者分布、严重度分布与口径说明；`--out brief.md` 可直接落盘粘进科务会材料，`--json` 输出原始聚合供 BI 二次统计。

## 架构与数据流

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
  ├─ index_builder.py       │  (frontmatter 解析 + 去重 + 类别校验)
  ├─ runtime.py             │  (token 重叠打分 + severity 排序 + fuzzy)
  ├─ tools.py               │  (4 工具 schema + apply_rule 审方意见)
  ├─ audit.py               │  (JSONL append + 月度归档 + summary)
  └─ contract.py            │  (HIS 字段 JSON Schema)
    │                       │
    ▼                       │
cli.py   ──►  pass-fc 入口  │
api.py   ──►  FastAPI 8765  │
    │                       │
    └─────────► audit/audit.jsonl
                    │
                    ▼
            pass-fc audit-export / audit-summary
```

主路径：`rules/*.md` → `rx-index-build` → `data/rules.json` → `runtime.search_rules` → 4 工具（search / inspect / load / apply）→ `audit.jsonl` → 月度导出 / 简报。

## 安全与合规边界

- **不输出诊断或治疗建议**：本工作台仅做「规则命中 + 依据片段 + 严重度」的事实呈现，所有 `confirmed` 字段默认 `false`，由审方 / 临床药师人工确认后推进。
- **不自动写回 HIS 审方栏**：apply_rule 仅生成带 `order_hash` / `session_id` 的审方意见 JSON，是否写回由 HIS 工作站决定；mock 替身仅用于本地测试。
- **审计可追溯**：所有 search / inspect / load / apply 行为以 ISO 8601 时间戳 + `operator` 留痕，月度归档 / 旋转避免 JSONL 膨胀。

## 项目结构

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
│   ├── tools.py                # 4 工具 schema + register_rx_tools
│   ├── audit.py                # append_audit / export_month / query_events / monthly_summary
│   ├── contract.py             # HIS 审方栏字段 JSON Schema
│   ├── api.py                  # FastAPI 5 路由
│   └── cli.py                  # pass-fc 入口 + 8 子命令
├── rules/
│   └── rx-*.md                 # 药事委员会维护的合理用药规则（≥12 条样本）
├── scripts/
│   ├── rx-index-build.py       # 构建 data/rules.json
│   ├── check-rx-rule-workbench.py  # 4 工具契约 + search→inspect 烟测
│   └── demo.sh                 # 一行验证全链路
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
├── data/                       # 构建产物（runtime 生成，已 gitignore）
├── audit/                      # JSONL 审计（runtime 生成，已 gitignore）
├── logs/                       # 运行日志（runtime 生成，已 gitignore）
└── docs/screenshots/
```

## License

MIT

---

## 关注我们

欢迎扫码关注公众号，获取项目更新与交流加群：

![关注我们](qrcode.jpg)