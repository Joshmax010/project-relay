#!/bin/sh
# project-relay installer: clone the repo (or fast-forward it) and run
# `recipe install`, so the machine gets the relay engine without cloning
# by hand.
#
#   curl -fsSL https://raw.githubusercontent.com/Joshmax010/project-relay/main/scripts/install.sh | sh -s -- [--tool TOOL] [--dir DIR]
#
# --tool: claude-code | codex | codebuddy | workbuddy. Without it, the
#   script picks the one agent whose config dir already exists under $HOME;
#   if none or more than one exists, it stops and asks for --tool.
# --dir: where the repo is cloned (default: $HOME/.local/share/project-relay).
#   An existing checkout is fast-forwarded with `git pull` instead of cloned.
# Env overrides (tests, offline use): RELAY_REPO_URL, RELAY_DIR, RELAY_TOOL.
# Needs: git, python3 (or python), sh. Nothing else is installed.
#
# Codex is per-project: its managed section lands in the clone's AGENTS.md,
# so after this script, run the recipe install once more from your own
# project directory (the script prints the exact command).

set -eu

REPO_URL="${RELAY_REPO_URL:-https://github.com/Joshmax010/project-relay.git}"
DIR="${RELAY_DIR:-$HOME/.local/share/project-relay}"
TOOL="${RELAY_TOOL:-}"

usage() {
    echo "用法: install.sh [--tool claude-code|codex|codebuddy|workbuddy] [--dir DIR]"
    echo "  不传 --tool 时按 \$HOME 下已有的 agent 配置目录自动判断，判断不出就停下等你指定。"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tool) TOOL="$2"; shift 2 ;;
        --tool=*) TOOL="${1#--tool=}"; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        --dir=*) DIR="${1#--dir=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$TOOL" in
    ""|claude-code|codex|codebuddy|workbuddy) ;;
    *) echo "未知的 --tool：$TOOL（只要 claude-code、codex、codebuddy、workbuddy）" >&2; exit 2 ;;
esac

if [ -z "$TOOL" ]; then
    found=""
    count=0
    for pair in "claude-code:$HOME/.claude" "codex:$HOME/.codex" "codebuddy:$HOME/.codebuddy" "workbuddy:$HOME/.workbuddy"; do
        name="${pair%%:*}"
        marker="${pair#*:}"
        if [ -d "$marker" ]; then
            found="$name"
            count=$((count + 1))
        fi
    done
    if [ "$count" -eq 1 ]; then
        TOOL="$found"
        echo "按 $HOME 下的配置目录判断，装到 $TOOL。"
    else
        echo "判断不出装到哪个 agent（\$HOME 下有 $count 个 agent 配置目录），请指定 --tool。" >&2
        usage >&2
        exit 2
    fi
fi

command -v git >/dev/null 2>&1 || { echo "找不到 git，请先装 git。" >&2; exit 1; }
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "找不到 python3/python，请先装 Python 3.8+。" >&2
    exit 1
fi

if [ -d "$DIR/.git" ]; then
    echo "已存在 $DIR，git pull 快进。"
    git -C "$DIR" pull --ff-only
elif [ -e "$DIR" ]; then
    echo "$DIR 已存在但不是 git 仓库，删掉或换个 --dir 再跑。" >&2
    exit 1
else
    echo "克隆 $REPO_URL 到 $DIR。"
    git clone "$REPO_URL" "$DIR"
fi

echo "跑 recipe install --tool $TOOL 前先建仓（recipe install 要求项目已 init）。"
"$PY" "$DIR/scripts/relay.py" --root "$DIR" init

echo "跑 recipe install --tool $TOOL。"
"$PY" "$DIR/scripts/relay.py" --root "$DIR" recipe install --tool "$TOOL"

echo "装好了：$TOOL。"
if [ "$TOOL" = "codex" ]; then
    echo "Codex 是按项目装的：受管小节写进了 $DIR/AGENTS.md。在你自己的项目里再跑一次："
    echo "  python3 $DIR/scripts/relay.py --root <你的项目目录> recipe install --tool codex"
fi
