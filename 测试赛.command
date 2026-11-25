#!/bin/bash
# 双击运行：参加你在平台 Testing 页面开的测试局。日志保存在 logs/ 文件夹。
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/test-$(date +%m%d-%H%M).txt"
if [ -d .git ]; then
  if git pull --ff-only -q origin main; then echo "✓ 代码已更新到最新：$(git log -1 --format='%h %s')";
  else echo "⚠️ 没能自动更新代码，继续用当前版本。"; fi
fi
source .venv/bin/activate
echo "测试赛模式启动。去平台 Testing 页面开一局（座位选 Mine (self-hosted)）。"
echo "日志同时保存在 $LOG 。按 Ctrl+C 停止。"
python -u -m agent --match 2>&1 | tee "$LOG"
read -p "已停止，按回车关闭窗口"
