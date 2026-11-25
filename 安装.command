#!/bin/bash
# 一键安装和检查：双击运行。结果同时保存在“安装结果.txt”。
cd "$(dirname "$0")"
exec > >(tee "安装结果.txt") 2>&1

echo "== 1. 检查 Python 版本（需要 3.11+）=="
PY=""
for c in python3.13 python3.12 python3.11 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if [ "$("$c" -c 'import sys;print(sys.version_info>=(3,11))' 2>/dev/null)" = "True" ]; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 没找到 3.11 或更高版本的 Python。当前版本："
  python3 --version 2>&1
  echo "请到 https://www.python.org/downloads/ 下载安装最新版，装好后再双击本文件。"
  read -p "按回车关闭窗口"; exit 1
fi
"$PY" --version
echo "✓ Python 可以用"

echo; echo "== 2. 安装运行环境（第一次大约 1–3 分钟）=="
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
python -m pip install -q --upgrade pip
if python -m pip install -q -e ".[dev]"; then echo "✓ 安装完成"; else echo "❌ 安装失败（看上面的错误）"; fi

echo; echo "== 3. 跑我们的测试（应显示 17 passed）=="
python -m pytest tests/test_our_agent.py -q 2>&1 | tail -3

echo; echo "== 4. 测试 OpenAI key（不会显示 key 本身）=="
python - <<'EOF'
import os, re
from dotenv import load_dotenv
load_dotenv(".env")
key = os.environ.get("OPENAI_API_KEY", "")
if not key or key.startswith("sk-your"):
    print("❌ .env 里的 OPENAI_API_KEY 还是空的或占位符")
else:
    try:
        from examples.llm.providers import provider_from_env
        p = provider_from_env()
        schema = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"], "additionalProperties": False}
        out = p.complete_structured([{"role": "user", "content": "Reply with ok"}], "test", schema)
        print(f"✓ OpenAI key 可以用（模型：{getattr(p, 'model', '?')}，回答：{out}）")
    except Exception as e:
        msg = re.sub(r"sk-[A-Za-z0-9_\-\*]+", "sk-***", str(e))
        print(f"❌ 调用失败：{type(e).__name__}: {msg[:400]}")
EOF

echo; echo "== 5. 比赛平台 key =="
if grep -q "^ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_your_key_here" .env; then
  echo "（还没填比赛平台的 key，之后再检查）"
else
  python -m agent --check-tournament 2>&1 | sed -E 's/eak_live_[A-Za-z0-9_]+/eak_live_***/g' | tail -15
fi

echo; echo "== 完成。可以回到 Claude 说“好了” =="
read -p "按回车关闭窗口"
