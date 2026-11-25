#!/bin/bash
# 双击运行：12月2日正式比赛用。全程不要关这个窗口、不要让电脑睡眠。
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/tournament-$(date +%m%d-%H%M).txt"
source .venv/bin/activate
echo "正式比赛模式启动。先确认平台 Tournaments 页面已点 Register my agent。"
echo "日志同时保存在 $LOG 。比赛结束前不要关闭窗口。"
caffeinate -dimsu python -u -m agent --tournament 2>&1 | tee "$LOG"
read -p "已停止。如果比赛还没结束，请重新双击本文件。按回车关闭窗口"
