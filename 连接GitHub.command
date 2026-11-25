#!/bin/bash
# 只需运行一次：把这个文件夹和 GitHub 仓库连起来（不会改动你的 .env 和现有文件）。结果存在“连接结果.txt”。
cd "$(dirname "$0")"
exec > >(tee "连接结果.txt") 2>&1
REPO="https://github.com/gabrielletang/ucla-agent-game.git"
if ! git --version >/dev/null 2>&1; then
  echo "❌ 这台电脑还没有 git。马上会弹出安装窗口，点“安装”，装好后再双击本文件。"
  xcode-select --install 2>/dev/null
  read -p "按回车关闭窗口"; exit 1
fi
git --version
if [ -d .git ]; then
  echo "✓ 这个文件夹已经连着 GitHub 了"
else
  git init -q -b main && git remote add origin "$REPO" && git fetch -q origin main && git reset -q origin/main \
    && git branch -q --set-upstream-to=origin/main main && echo "✓ 已连接到 $REPO"
fi
echo; echo "== 和 GitHub 上最新版本的差异（空白表示完全一致）=="
git status --short
echo; echo "== 完成。可以回到 Claude 说“好了” =="
read -p "按回车关闭窗口"
