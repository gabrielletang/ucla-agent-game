#!/bin/bash
# 双击：从 GitHub 拿最新代码（不会动 .env）。正式比赛进行中不要运行。
cd "$(dirname "$0")"
if [ ! -d .git ]; then echo "请先双击“连接GitHub.command”"; read -p "按回车关闭窗口"; exit 1; fi
echo "正在从 GitHub 获取最新代码..."
if git pull --ff-only -q origin main; then
  echo "✓ 已是最新：$(git log -1 --format='%h %s')"
else
  echo "❌ 没能自动更新：这台电脑上有人改过代码文件，和 GitHub 上的版本冲突。把这个窗口截图发给 Claude。"
  git status --short
fi
read -p "按回车关闭窗口"
