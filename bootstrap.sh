#!/usr/bin/env bash
# lark-bug-pipeline bootstrap
#
# 一行命令把 skill 下载到当前仓库的 .claude/skills/lark-bug-pipeline/
# 然后打印下一步操作。不会自动改你的环境。
#
# 用法：
#   cd <your-repo>
#   curl -fsSL https://raw.githubusercontent.com/NatureBlueee/lark-bug-pipeline/main/bootstrap.sh | bash
#
# 或者让你的 Claude Code 读这个脚本并代你执行。

set -euo pipefail

REPO_URL="https://github.com/NatureBlueee/lark-bug-pipeline.git"
TARGET=".claude/skills/lark-bug-pipeline"

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "[FAIL] 当前目录不在 git 仓库内。请 cd 到你的项目根再运行。" >&2
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ -d "$TARGET" ]]; then
    echo "[INFO] $TARGET 已存在，拉取最新 main"
    # 如果之前是 git clone 来的，直接 fetch + reset
    if [[ -d "$TARGET/.git" ]]; then
        git -C "$TARGET" fetch --depth 1 origin main
        git -C "$TARGET" reset --hard origin/main
    else
        # 之前用的是 tarball / 手动拷贝——备份再覆盖
        BACKUP="$TARGET.bak.$(date +%s)"
        echo "[WARN] 现有目录不是 git clone 产物，备份为 $BACKUP"
        mv "$TARGET" "$BACKUP"
        git clone --depth 1 "$REPO_URL" "$TARGET"
        rm -rf "$TARGET/.git"
    fi
else
    echo "[INFO] clone $REPO_URL → $TARGET"
    mkdir -p "$(dirname "$TARGET")"
    git clone --depth 1 "$REPO_URL" "$TARGET"
    rm -rf "$TARGET/.git"   # 不把子 skill 的 git 元数据带进你的母仓库
fi

cat <<EOF

=== lark-bug-pipeline 已下载 ===

下一步（3 步装完）：

  1. 复制环境变量模板
     mkdir -p ~/.towow
     cp $TARGET/templates/env.lark.example ~/.towow/.env.lark

  2. 编辑 ~/.towow/.env.lark 填 4 个必填变量
     - LARK_APP_ID / LARK_APP_SECRET（从飞书自建应用拿）
     - LARK_BOT_OPEN_ID（第一次 @bot 后从 daemon 日志抄）
     - LARK_NATURE_OPEN_ID（你自己的 open_id，同上）
     飞书侧从零配置步骤：$TARGET/docs/feishu-setup.md

  3. 一键安装 LaunchAgent
     bash $TARGET/install.sh

不想一步步查文档？
  跟你的 Claude Code 说："用 lark-bug-pipeline skill 引导我装一下"
  skill 里的「AI 指导模式」会让 Claude 扮演"刚帮别人装过的工程师朋友"
  一步步问你要信息、告诉你 bot open_id 去哪抄、不吐术语不倒日志。

EOF
