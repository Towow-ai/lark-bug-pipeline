#!/usr/bin/env bash
# lark-bug-pipeline installer
# 把 runtime 脚本装到当前仓库的 scripts/lark/，渲染 LaunchAgent plist，
# bootstrap 两个 daemon，验证存活。
#
# 用法：
#   cd <your-repo>
#   bash .claude/skills/lark-bug-pipeline/install.sh
#
# 前置条件：
#   1. macOS
#   2. python3 + pip install lark-oapi
#   3. claude CLI 已登录 (which claude)
#   4. ~/.towow/.env.lark 已填好（从 templates/env.lark.example 复制）

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(pwd)"
HOME_DIR="$HOME"

echo "=== lark-bug-pipeline installer ==="
echo "Skill dir: $SKILL_DIR"
echo "Repo root: $REPO_ROOT"
echo ""

# --- 1. 前置检查 ---
if [[ "$(uname)" != "Darwin" ]]; then
    echo "[FAIL] 当前只支持 macOS (launchd)。Linux 用户请参考 docs/architecture.md 改用 systemd。"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "[FAIL] python3 not found"
    exit 1
fi
PYTHON="$(command -v python3)"

if ! command -v claude >/dev/null 2>&1; then
    echo "[FAIL] claude CLI not found. 先安装 Claude Code 并登录: https://claude.com/claude-code"
    exit 1
fi
CLAUDE_PATH="$(command -v claude)"
CLAUDE_DIR="$(dirname "$CLAUDE_PATH")"

if ! python3 -c "import lark_oapi" >/dev/null 2>&1; then
    echo "[WARN] lark-oapi 未安装，正在 pip install..."
    python3 -m pip install --user lark-oapi
fi

if [[ ! -f "$HOME_DIR/.towow/.env.lark" ]]; then
    echo "[FAIL] $HOME_DIR/.towow/.env.lark 不存在"
    echo "       先复制: cp $SKILL_DIR/templates/env.lark.example $HOME_DIR/.towow/.env.lark"
    echo "       然后按注释填实际值（详见 docs/feishu-setup.md）"
    exit 1
fi

# --- 2. 目标路径 ---
TARGET_SCRIPTS="$REPO_ROOT/scripts/lark"
mkdir -p "$TARGET_SCRIPTS" "$HOME_DIR/.towow/logs" "$HOME_DIR/Library/LaunchAgents"

# --- 3. 复制 runtime 脚本 ---
cp "$SKILL_DIR/runtime/bug_daemon.py" "$TARGET_SCRIPTS/bug_daemon.py"
cp "$SKILL_DIR/runtime/bug_worker.py" "$TARGET_SCRIPTS/bug_worker.py"
echo "[OK] runtime 脚本已装到 $TARGET_SCRIPTS"

# --- 4. 装 lark-triage 子 skill ---
LARK_TRIAGE_DIR="$REPO_ROOT/.claude/skills/lark-triage"
if [[ ! -f "$LARK_TRIAGE_DIR/SKILL.md" ]]; then
    mkdir -p "$LARK_TRIAGE_DIR"
    cp "$SKILL_DIR/runtime/lark-triage.md" "$LARK_TRIAGE_DIR/SKILL.md"
    echo "[OK] lark-triage skill 已装"
else
    echo "[SKIP] lark-triage skill 已存在，未覆盖"
fi

# --- 5. 渲染 LaunchAgent plist ---
PATH_PREFIX="$CLAUDE_DIR:/opt/homebrew/bin"

render_plist() {
    local src="$1" dst="$2"
    sed \
        -e "s|{{PYTHON}}|$PYTHON|g" \
        -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" \
        -e "s|{{HOME}}|$HOME_DIR|g" \
        -e "s|{{PATH_PREFIX}}|$PATH_PREFIX|g" \
        "$src" > "$dst"
}

render_plist \
    "$SKILL_DIR/templates/net.towow.lark-daemon.plist.tmpl" \
    "$HOME_DIR/Library/LaunchAgents/net.towow.lark-daemon.plist"
render_plist \
    "$SKILL_DIR/templates/net.towow.lark-worker.plist.tmpl" \
    "$HOME_DIR/Library/LaunchAgents/net.towow.lark-worker.plist"
echo "[OK] LaunchAgent plist 已渲染"

# --- 6. bootstrap ---
UID_NUM=$(id -u)
for label in net.towow.lark-daemon net.towow.lark-worker; do
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$HOME_DIR/Library/LaunchAgents/$label.plist"
done
echo "[OK] LaunchAgent bootstrap 完成"

sleep 3

# --- 7. 验证 ---
for label in net.towow.lark-daemon net.towow.lark-worker; do
    state=$(launchctl print "gui/$UID_NUM/$label" 2>/dev/null | awk '/^\tstate = /{print $3; exit}')
    if [[ "$state" == "running" ]]; then
        echo "[OK] $label state=running"
    else
        echo "[WARN] $label state=$state — 查看日志:"
        echo "       tail -30 $HOME_DIR/.towow/logs/${label#net.towow.}.err"
    fi
done

cat <<'EOF'

=== 安装完成 ===

下一步：
  1. 飞书表格里 @bot 发一条 bug 试试
  2. 看日志:
     tail -f ~/.towow/logs/lark-worker.log ~/.towow/logs/lark-daemon.err
  3. 最多 30 秒后 worker 会 pick up；完整闭环约 15 分钟到 PR

运维命令:
  # 重启
  launchctl kickstart -k gui/$(id -u)/net.towow.lark-daemon
  launchctl kickstart -k gui/$(id -u)/net.towow.lark-worker

  # 卸载
  launchctl bootout gui/$(id -u)/net.towow.lark-daemon
  launchctl bootout gui/$(id -u)/net.towow.lark-worker

EOF
