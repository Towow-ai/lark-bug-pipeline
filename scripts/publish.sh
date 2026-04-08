#!/usr/bin/env bash
# 打包 lark-bug-pipeline skill 成可分发 tarball。
# 产物：dist/lark-bug-pipeline-v<VERSION>.tar.gz
#
# 用法：
#   cd .claude/skills/lark-bug-pipeline
#   bash scripts/publish.sh
#
# 产物解压后会得到 lark-bug-pipeline/ 目录，直接拖到目标仓库的
# .claude/skills/ 下就能用。

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SKILL_DIR"

VERSION="$(cat VERSION | tr -d '[:space:]')"
if [[ -z "$VERSION" ]]; then
    echo "[FAIL] VERSION 文件为空" >&2
    exit 1
fi

# dist/ 放在仓库根，避免 skill 内部自包含（tarball 解压时不要又出现一层 dist）
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$SKILL_DIR/../../..")"
DIST_DIR="$REPO_ROOT/dist"
mkdir -p "$DIST_DIR"

ARTIFACT="lark-bug-pipeline-v${VERSION}.tar.gz"
ARTIFACT_PATH="$DIST_DIR/$ARTIFACT"

# 清理 pycache / DS_Store
find "$SKILL_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SKILL_DIR" -name .DS_Store -type f -delete 2>/dev/null || true

# tar 时用父目录做 cwd，这样 tarball 里是 lark-bug-pipeline/... 而不是 ./...
PARENT="$(dirname "$SKILL_DIR")"
BASENAME="$(basename "$SKILL_DIR")"

tar -czf "$ARTIFACT_PATH" \
    --exclude="$BASENAME/.DS_Store" \
    --exclude="$BASENAME/__pycache__" \
    --exclude="$BASENAME/scripts/publish.sh" \
    -C "$PARENT" \
    "$BASENAME"

SIZE="$(du -h "$ARTIFACT_PATH" | cut -f1)"
SHA="$(shasum -a 256 "$ARTIFACT_PATH" | cut -d' ' -f1)"

cat <<EOF

=== Publish OK ===
  Artifact: $ARTIFACT_PATH
  Size:     $SIZE
  SHA256:   $SHA
  Version:  v$VERSION

下一步（可选）：
  1. 创建独立 GitHub 仓库:
       gh repo create lark-bug-pipeline --public \\
         --description "Feishu group @bot → auto GitHub PR in 15min via Claude Code harness"
     然后把 $SKILL_DIR 作为仓库根 push 上去（README.md 已就位）
  2. GitHub Release:
       gh release create v$VERSION $ARTIFACT_PATH \\
         --title "lark-bug-pipeline v$VERSION" \\
         --notes-file CHANGELOG.md
EOF
