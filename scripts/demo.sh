#!/usr/bin/env bash
# scripts/demo.sh — 一行跑通全链路:
#   install -> index-build -> search -> inspect -> apply -> audit-export
# 设计原则:幂等可重跑,失败立即退出(stderr + exit != 0),结尾输出 "demo ok"。
# 调用方式: bash scripts/demo.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# 路径解析:无论从哪个 cwd 调用,都基于脚本自身位置定位项目根
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON="${PYTHON:-python}"

# pass-fc:函数封装避免在 $() 子 shell 里丢参数
pass_fc() {
  "${PYTHON}" -m pass_field_check.cli "$@"
}

RULES_DIR="${RX_RULES_DIR:-./rules}"
INDEX_FILE="${RX_INDEX_FILE:-./rules_index.json}"
DATA_DIR="${RX_DATA_DIR:-./data}"
AUDIT_PATH="${RX_AUDIT_PATH:-./audit/audit.jsonl}"
CURRENT_MONTH="$(date +%Y-%m)"

echo "=== demo.sh · 用药字段对照工作台 ==="
echo "项目根: ${PROJECT_ROOT}"
echo "Python: $($PYTHON --version 2>&1)"
echo

# ---------------------------------------------------------------------------
# 1. 依赖检查(若缺失则 install — 幂等,已装即跳过)
# ---------------------------------------------------------------------------
echo "[1/6] 依赖检查"
if ! "${PYTHON}" -c "import yaml, fastapi, pydantic, jsonschema" >/dev/null 2>&1; then
  echo "  → 安装依赖 (pip install -r requirements.txt)"
  "${PYTHON}" -m pip install -q -r requirements.txt
else
  echo "  → 已安装(yaml / fastapi / pydantic / jsonschema)"
fi
echo

# ---------------------------------------------------------------------------
# 2. index-build:从 rules/*.md 重建 data/rules.json(清空旧产物,保证幂等)
# ---------------------------------------------------------------------------
echo "[2/6] index-build(重建 data/rules.json)"
rm -f "${DATA_DIR}/rules.json" "${DATA_DIR}/rules.json.tmp"
"${PYTHON}" scripts/rx-index-build.py \
  --rules-dir "${RULES_DIR}" \
  --out "${DATA_DIR}/rules.json" \
  --index "${INDEX_FILE}"
echo

# ---------------------------------------------------------------------------
# 3. search:用经典 query 命中 top-N 规则(取 top-1 验证)
# ---------------------------------------------------------------------------
echo "[3/6] search: '庆大霉素 儿童 8 岁'"
SEARCH_OUTPUT="$(pass_fc search '庆大霉素 儿童 8 岁' --limit 5)"
echo "${SEARCH_OUTPUT}"
TOP1_RULE_ID="$(echo "${SEARCH_OUTPUT}" | awk 'NR==3 {print $1}')"
if [[ -z "${TOP1_RULE_ID}" || "${TOP1_RULE_ID}" == "(no"* ]]; then
  echo "error: search 未命中任何规则" >&2
  exit 1
fi
echo "  → top-1 rule_id = ${TOP1_RULE_ID}"
echo

# ---------------------------------------------------------------------------
# 4. inspect:查看 top-1 规则全文(body 默认含)
# ---------------------------------------------------------------------------
echo "[4/6] inspect: --rule-id ${TOP1_RULE_ID}"
pass_fc inspect --rule-id "${TOP1_RULE_ID}" --include-body | head -40
echo

# ---------------------------------------------------------------------------
# 5. apply:生成审方意见 JSON + audit.jsonl 留痕(confirmed=False 必显式)
# ---------------------------------------------------------------------------
echo "[5/6] apply: --operator pharmacist-demo"
ORDER_CONTEXT='{"drug":"庆大霉素","age":8,"egfr":90,"route":"iv","dose_mg":80}'
APPLY_OUTPUT="$(pass_fc apply \
  --rule-id "${TOP1_RULE_ID}" \
  --order-context "${ORDER_CONTEXT}" \
  --operator pharmacist-demo)"
echo "${APPLY_OUTPUT}"
if ! echo "${APPLY_OUTPUT}" | grep -q '"confirmed": false'; then
  echo "error: apply 输出未显式 confirmed=false(违反安全边界)" >&2
  exit 1
fi
echo "  → audit.jsonl 已追加 1 行"
echo

# ---------------------------------------------------------------------------
# 6. audit-export:按月导出 CSV(utf-8-sig BOM,Excel 可直开)
# ---------------------------------------------------------------------------
echo "[6/6] audit-export --month ${CURRENT_MONTH}"
EXPORT_OUT="${DATA_DIR}/../audit/audit-${CURRENT_MONTH}.csv"
pass_fc audit-export --month "${CURRENT_MONTH}" --out "${EXPORT_OUT}"
echo

# ---------------------------------------------------------------------------
# 收尾:验证 CSV 含 BOM + 列头 + 至少 1 行数据
# ---------------------------------------------------------------------------
if [[ ! -f "${EXPORT_OUT}" ]]; then
  echo "error: 导出 CSV 不存在: ${EXPORT_OUT}" >&2
  exit 1
fi
FIRST_BYTES="$(head -c 3 "${EXPORT_OUT}" | xxd -p)"
if [[ "${FIRST_BYTES}" != "efbbbf" ]]; then
  echo "error: CSV 首字节非 utf-8-sig BOM(实际 ${FIRST_BYTES})" >&2
  exit 1
fi
DATA_ROWS="$(($(wc -l < "${EXPORT_OUT}") - 1))"
if (( DATA_ROWS < 1 )); then
  echo "error: CSV 数据行数 < 1" >&2
  exit 1
fi

echo "=== demo ok · ${DATA_ROWS} 条 audit 事件已导出到 ${EXPORT_OUT} ==="
