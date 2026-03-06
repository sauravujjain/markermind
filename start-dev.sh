#!/bin/bash
# MarkerMind dev server launcher — handles remote access
set -e

HOST_IP=$(hostname -I | awk '{print $1}')
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "=== MarkerMind Dev Servers ==="
echo "Host IP: $HOST_IP"
echo ""

# Kill any existing servers
fuser -k 3000/tcp 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
sleep 1

# Start backend (bound to all interfaces)
echo "Starting backend on 0.0.0.0:8000..."
cd "$PROJECT_DIR/backend"
source venv/bin/activate
uvicorn backend.main:app --reload --host 0.0.0.0 > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!

# Start frontend with API URL pointing to host IP
echo "Starting frontend on 0.0.0.0:3000..."
cd "$PROJECT_DIR/frontend"
NEXT_PUBLIC_API_URL="http://${HOST_IP}:8000/api" npx next dev -H 0.0.0.0 > "$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!

# Wait for servers to be ready
sleep 4

echo ""
echo "=== Ready ==="
echo "  Frontend:  http://${HOST_IP}:3000"
echo "  Backend:   http://${HOST_IP}:8000"
echo "  API docs:  http://${HOST_IP}:8000/docs"
echo ""
echo "  Logs: tail -f $LOG_DIR/backend.log $LOG_DIR/frontend.log"
echo "  Stop: kill $BACKEND_PID $FRONTEND_PID"
echo ""
