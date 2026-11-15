#!/bin/sh
# project-relay installer (DeepSeek Harness edition): clone or fast-forward
# the repo, then install the project-relay skill where DSH scans it.
#
#   curl -fsSL .../scripts/install.sh | sh -s -- [--dir DIR]
#
# --dir: where the repo is cloned (default: $HOME/.local/share/project-relay).
# Env overrides: RELAY_REPO_URL, RELAY_DIR.
# Needs: git, python3 (or python), sh.

set -eu

REPO_URL="${RELAY_REPO_URL:-https://github.com/Joshmax010/project-relay.git}"
DIR="${RELAY_DIR:-$HOME/.local/share/project-relay}"

usage() {
    echo "用法: install.sh [--dir DIR]"
    echo "本版只安装 DeepSeek Harness 的技能；其他 Agent 不再适配。"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="$2"; shift 2 ;;
        --dir=*) DIR="${1#--dir=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

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

echo "安装 project-relay 到 DeepSeek Harness（用户级 ~/.agents/skills/project-relay）。"
"$PY" "$DIR/scripts/relay.py" --root "$DIR" init
"$PY" "$DIR/scripts/relay.py" --root "$DIR" recipe install --tool deepseek-harness --home "$HOME"
echo "装好了：DeepSeek Harness。"
