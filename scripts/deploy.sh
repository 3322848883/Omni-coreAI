#!/bin/bash
# ============================================================================
# gate-signal-bot 部署流水线（本地/服务器通用）
#
# 七步：前置检查 → 拉代码 → 依赖 → 同步 skill → 编码自检 → 测试 → 重启+验证
#
# 用法：
#   ./scripts/deploy.sh              # 完整部署（含重启）
#   ./scripts/deploy.sh --no-restart # 只同步不重启（先验证）
#   ./scripts/deploy.sh --dry-run    # 只打印将执行的动作
#
# 关键安全：第 6 步测试失败 → 中止且【不重启】，保留旧进程继续跑。
# 环境差异：config/bots.local/ 为 gitignore 的 overlay，pull 不会覆盖它。
# ============================================================================
set -euo pipefail

ROOT="${GATE_BOT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

NO_RESTART=0
DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --no-restart) NO_RESTART=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    *) echo "unknown arg: $a"; exit 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then echo "  [dry-run] $*"; else echo "  + $*"; "$@"; fi
}

echo "=============================================="
echo "gate-signal-bot 部署 · root=$ROOT"
echo "=============================================="

# ── 1) 前置检查 ────────────────────────────────
echo
echo "[1/7] 前置检查"
DIRTY=$(git status --porcelain | grep -v -E '^\?\? (config/bots\.local/|data/|skills/|logs/|verify_data/)|^ M config/bots\.local/' || true)
if [ -n "$DIRTY" ]; then
  echo "  ⛔ 工作区有未提交改动（应只允许 bots.local/ 与 data/）："
  echo "$DIRTY" | sed 's/^/     /'
  echo "  请先提交或 stash，再部署。"
  exit 1
fi
echo "  OK 工作区干净（除 overlay/runtime）"
echo "  overlay: $(ls config/bots.local/*.yaml 2>/dev/null | wc -l) 个"

# ── 2) 拉取代码 ────────────────────────────────
echo
echo "[2/7] 拉取代码"
run git pull --ff-only origin master
if [ "$DRY_RUN" = "0" ]; then echo "  HEAD: $(git log --oneline -1)"; fi

# ── 3) 依赖 ───────────────────────────────────
echo
echo "[3/7] 依赖"
if command -v uv >/dev/null 2>&1; then
  run uv pip install -q --python "$PY" -r requirements.txt
  run uv pip install -q --python "$PY" -e .
else
  run "$PY" -m pip install -q -r requirements.txt
  run "$PY" -m pip install -q -e .
fi

# ── 4) 同步 skill ─────────────────────────────
echo
echo "[4/7] 同步 skill（skills-src/ → skills/）"
if [ -d skills-src ]; then
  for d in skills-src/*/; do
    [ -d "$d" ] || continue
    id=$(basename "$d")
    echo "  -- $id"
    if [ "$DRY_RUN" = "1" ]; then
      echo "     [dry-run] validate + install $id"
    else
      "$PY" -m gate_bot skill validate "$d" >/dev/null || { echo "     ⛔ validate 失败，中止"; exit 1; }
      "$PY" -m gate_bot skill install "$d" --yes >/dev/null
      echo "     installed"
    fi
  done
else
  echo "  (无 skills-src/，跳过)"
fi

# ── 5) 编码自检 ───────────────────────────────
echo
echo "[5/7] 编码自检（乱码目录名）"
if [ "$DRY_RUN" = "1" ]; then
  echo "  [dry-run] skill doctor --fix"
else
  "$PY" -m gate_bot skill doctor --fix || echo "  ⚠️  doctor 报告问题（见上）"
fi

# ── 6) 测试（失败即中止，不重启）──────────────
echo
echo "[6/7] 全量测试"
if [ "$DRY_RUN" = "1" ]; then
  echo "  [dry-run] unittest discover -s tests"
else
  if ! "$PY" -m unittest discover -s tests 2>&1 | tail -4; then
    echo "  ⛔ 测试失败 → 中止部署（不重启，旧进程继续运行）"
    exit 1
  fi
fi

# ── 7) 重启 + 验证 ────────────────────────────
echo
echo "[7/7] 重启 + 验证"
if [ "$NO_RESTART" = "1" ]; then
  echo "  (--no-restart：跳过重启)"
elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q gate-watchdog; then
  run systemctl restart gate-watchdog
  if [ "$DRY_RUN" = "0" ]; then
    sleep 12
    echo "  service: $(systemctl is-active gate-watchdog)"
  fi
else
  echo "  (无 gate-watchdog 服务，跳过重启；如需手动：python -m gate_bot watchdog)"
fi

if [ "$DRY_RUN" = "0" ]; then
  echo
  "$PY" -m gate_bot deploy-check || true
fi

echo
echo "=============================================="
echo "部署完成"
echo "=============================================="
