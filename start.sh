#!/bin/bash
#
# MarkerMind - Start all services
# Usage: ./start.sh         (start all)
#        ./start.sh stop    (stop all)
#        ./start.sh restart (restart all)
#        ./start.sh status  (check status)
#

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$PROJECT_DIR/logs"
CONDA_ENV="nester"
CONDA_BASE="$HOME/miniconda3"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

mkdir -p "$LOG_DIR"

log() { echo -e "${BLUE}[MarkerMind]${NC} $1"; }
ok()  { echo -e "${GREEN}  ✓${NC} $1"; }
err() { echo -e "${RED}  ✗${NC} $1"; }
warn(){ echo -e "${YELLOW}  !${NC} $1"; }

# ── Status ───────────────────────────────────────────────────
check_status() {
    echo ""
    log "Service Status"
    echo "  ─────────────────────────────────────────"

    # PostgreSQL
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q markermind_postgres; then
        ok "PostgreSQL     :5432  (docker)"
    else
        err "PostgreSQL     not running"
    fi

    # Redis
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q markermind_redis; then
        ok "Redis          :6379  (docker)"
    else
        warn "Redis          not running (optional)"
    fi

    # Backend
    if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
        ok "Backend API    :8000  (uvicorn)"
    else
        err "Backend API    not running"
    fi

    # Frontend
    if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1; then
        ok "Frontend       :3000  (next.js)"
    else
        err "Frontend       not running"
    fi

    echo "  ─────────────────────────────────────────"
    echo ""

    # Show URLs if everything is up
    if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1 && lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
        log "App ready at: ${GREEN}http://localhost:3000${NC}"
        log "API docs at:  ${GREEN}http://localhost:8000/docs${NC}"
    fi
    echo ""
}

# ── Stop ─────────────────────────────────────────────────────
stop_services() {
    log "Stopping services..."

    # Kill backend (uvicorn on port 8000)
    if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
        kill $(lsof -t -i :8000) 2>/dev/null || true
        sleep 1
        # Force kill if still running
        if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
            kill -9 $(lsof -t -i :8000) 2>/dev/null || true
        fi
        ok "Backend stopped"
    else
        warn "Backend was not running"
    fi

    # Kill frontend (next.js on port 3000)
    if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1; then
        kill $(lsof -t -i :3000) 2>/dev/null || true
        sleep 1
        if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1; then
            kill -9 $(lsof -t -i :3000) 2>/dev/null || true
        fi
        ok "Frontend stopped"
    else
        warn "Frontend was not running"
    fi

    echo ""
}

# ── Start ────────────────────────────────────────────────────
start_services() {
    log "Starting MarkerMind..."
    echo ""

    # 1. Docker services (PostgreSQL + Redis)
    log "Starting databases..."
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q markermind_postgres; then
        # Try starting existing container first, fall back to docker-compose
        if docker start markermind_postgres >/dev/null 2>&1; then
            ok "PostgreSQL started (existing container)"
        else
            docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d postgres
            ok "PostgreSQL started (docker-compose)"
        fi
    else
        ok "PostgreSQL already running"
    fi

    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q markermind_redis; then
        if docker start markermind_redis >/dev/null 2>&1; then
            ok "Redis started (existing container)"
        else
            docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d redis
            ok "Redis started (docker-compose)"
        fi
    else
        ok "Redis already running"
    fi

    # Wait for PostgreSQL to be healthy
    log "Waiting for PostgreSQL..."
    for i in $(seq 1 15); do
        if docker exec markermind_postgres pg_isready -U markermind >/dev/null 2>&1; then
            ok "PostgreSQL healthy"
            break
        fi
        if [ "$i" -eq 15 ]; then
            err "PostgreSQL failed to start"
            exit 1
        fi
        sleep 1
    done

    # 2. Backend (uvicorn)
    log "Starting backend..."
    if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
        ok "Backend already running on :8000"
    else
        # Use conda env's Python directly (avoids subshell activation issues)
        PYTHON="${CONDA_BASE}/envs/${CONDA_ENV}/bin/python"
        cd "$BACKEND_DIR"
        $PYTHON -m backend.main > "$LOG_DIR/backend.log" 2>&1 &
        echo $! > "$LOG_DIR/backend.pid"

        # Wait for backend to be ready
        for i in $(seq 1 20); do
            if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
                ok "Backend started on :8000 (PID: $(cat "$LOG_DIR/backend.pid" 2>/dev/null))"
                break
            fi
            if [ "$i" -eq 20 ]; then
                err "Backend failed to start. Check $LOG_DIR/backend.log"
                exit 1
            fi
            sleep 1
        done
    fi

    # 3. Frontend (next.js production build + start)
    log "Starting frontend..."
    if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1; then
        ok "Frontend already running on :3000"
    else
        cd "$FRONTEND_DIR"

        # Always rebuild — stale builds cause hydration failures (unstyled
        # login page, broken JS) when code has changed since the last build.
        # The build is fast (~15s) and avoids hard-to-debug chunk mismatch issues.
        log "Building frontend (production)..."
        npx next build >> "$LOG_DIR/frontend.log" 2>&1
        if [ $? -ne 0 ]; then
            err "Frontend build failed. Check $LOG_DIR/frontend.log"
            err "Falling back to dev mode..."
            npx next dev -H 0.0.0.0 > "$LOG_DIR/frontend.log" 2>&1 &
            echo $! > "$LOG_DIR/frontend.pid"
        else
            ok "Frontend built successfully"
            npx next start -H 0.0.0.0 > "$LOG_DIR/frontend.log" 2>&1 &
            echo $! > "$LOG_DIR/frontend.pid"
        fi

        # Wait for frontend to be ready
        for i in $(seq 1 60); do
            if lsof -i :3000 -sTCP:LISTEN >/dev/null 2>&1; then
                ok "Frontend started on :3000 (PID: $(cat "$LOG_DIR/frontend.pid" 2>/dev/null))"
                break
            fi
            if [ "$i" -eq 60 ]; then
                err "Frontend failed to start. Check $LOG_DIR/frontend.log"
                exit 1
            fi
            sleep 1
        done
    fi

    echo ""
    check_status
}

# ── Main ─────────────────────────────────────────────────────
case "${1:-start}" in
    start)
        start_services
        ;;
    stop)
        stop_services
        check_status
        ;;
    restart)
        stop_services
        sleep 2
        start_services
        ;;
    status)
        check_status
        ;;
    logs)
        # Show recent logs
        echo "=== Backend (last 20 lines) ==="
        tail -20 "$LOG_DIR/backend.log" 2>/dev/null || echo "No backend log"
        echo ""
        echo "=== Frontend (last 20 lines) ==="
        tail -20 "$LOG_DIR/frontend.log" 2>/dev/null || echo "No frontend log"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
