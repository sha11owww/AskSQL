#!/usr/bin/env bash
# AskSQL 一键启动脚本（Git Bash / macOS / Linux）
# 用法：bash start.sh
# 说明：Windows 下若已用 Git Bash，可直接执行本脚本；PowerShell 用户请参考 README。

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "=========================================="
echo " AskSQL 启动中"
echo "=========================================="

# ---------- 1. 前置检查 ----------
if [ ! -f "backend/.env" ]; then
  echo "[错误] 未找到 backend/.env，请先复制 backend/.env.example 为 backend/.env 并填写 API Key"
  exit 1
fi

if ! grep -qE '^LLM_API_KEY=.+' backend/.env; then
  echo "[警告] backend/.env 中的 LLM_API_KEY 为空。"
  echo "       服务仍会启动，但模型调用会失败（健康检查的 api_key_configured 为 false）。"
  echo "       请填写 Key 后重启，否则查询会降级为不打库的保守回复。"
  echo ""
fi

if [ ! -f "backend/.venv/Scripts/python.exe" ] && [ ! -f "backend/.venv/bin/python" ]; then
  echo "[错误] 未找到 backend/.venv，请先创建虚拟环境并安装依赖："
  echo "       cd backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt"
  exit 1
fi

# ---------- 2. 选择可用的 Python ----------
if [ -f "backend/.venv/Scripts/python.exe" ]; then
  PY="./backend/.venv/Scripts/python.exe"
else
  PY="./backend/.venv/bin/python"
fi

# ---------- 3. 选择可用的 Node 运行时 ----------
if [ -x ".node/node.exe" ]; then
  NODE="./.node/node.exe"          # 项目自带的 Windows Node
elif [ -x ".node/bin/node" ]; then
  NODE="./.node/bin/node"          # 项目自带的 类 Unix Node
elif command -v node >/dev/null 2>&1; then
  NODE="node"                      # 回退到系统 Node
else
  echo "[错误] 未找到可用的 Node 运行时（需要 20.19+）"
  exit 1
fi

echo "使用 Python: $PY"
echo "使用 Node  : $NODE"
echo ""

# ---------- 4. 启动后端 ----------
echo "[1/2] 启动后端 http://127.0.0.1:8000 ..."
( cd backend && "../${PY#./}" run.py > ../backend/server.log 2>&1 ) &
BACKEND_PID=$!

# 等待后端就绪
for i in $(seq 1 30); do
  if curl -s -m 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo "      后端就绪 ✓"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "      [警告] 后端 30 秒内未就绪，请查看 backend/server.log"
  fi
  sleep 1
done

# ---------- 5. 启动前端 ----------
echo "[2/2] 启动前端 http://127.0.0.1:5173 ..."
( cd frontend && "../${NODE#./}" node_modules/vite/bin/vite.js > ../frontend/server.log 2>&1 ) &
FRONTEND_PID=$!

sleep 5

echo ""
echo "=========================================="
echo " 启动完成"
echo "=========================================="
echo " 前端页面  http://127.0.0.1:5173"
echo " API 文档  http://127.0.0.1:8000/docs"
echo " 健康检查  http://127.0.0.1:8000/api/health"
echo ""
echo " 内置账号  admin / admin123"
echo "            sales / sales123"
echo ""
echo " 按 Ctrl+C 停止全部服务"
echo "=========================================="

# ---------- 6. 退出时清理 ----------
cleanup() {
  echo ""
  echo "正在停止服务..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "已停止。"
}
trap cleanup EXIT INT TERM

wait
