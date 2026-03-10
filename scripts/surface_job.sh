#!/bin/bash
# =============================================================================
# Surface Nesting Job Manager
# Submit, monitor, and retrieve nesting jobs on the Surface Laptop 2.
#
# Usage:
#   ./surface_job.sh submit <jobs.json> [results.json]   — send job & start nesting
#   ./surface_job.sh status                               — check running job
#   ./surface_job.sh results [results.json]               — fetch results file
#   ./surface_job.sh test                                 — run 60s test job
#   ./surface_job.sh ssh                                  — open interactive SSH
# =============================================================================
set -euo pipefail

SURFACE="surface"  # Uses ~/.ssh/config entry
REMOTE_DIR="/home/nestworker"
LOCAL_RESULTS="/mnt/c/temp"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

check_connection() {
    if ! ssh -o ConnectTimeout=3 "$SURFACE" "echo ok" &>/dev/null; then
        error "Cannot connect to Surface. Check ethernet cable and SSH."
        echo "  Try: ping 192.168.50.2"
        echo "  Try: ssh nestworker@192.168.50.2"
        exit 1
    fi
}

cmd_submit() {
    local jobs_file="${1:?Usage: surface_job.sh submit <jobs.json> [results.json]}"
    local results_name="${2:-results_$(date +%Y%m%d_%H%M%S).json}"

    check_connection

    info "Uploading job file: $jobs_file"
    scp "$jobs_file" "$SURFACE:$REMOTE_DIR/"

    local remote_jobs="$REMOTE_DIR/$(basename "$jobs_file")"
    local remote_results="$REMOTE_DIR/$results_name"

    # Count jobs
    local n_jobs
    n_jobs=$(python3 -c "import json; print(len(json.load(open('$jobs_file'))))")
    info "Submitting $n_jobs jobs (results → $results_name)"

    # Upload latest worker script
    local worker_script="$(dirname "$0")/surface_nesting_worker.py"
    if [ -f "$worker_script" ]; then
        scp "$worker_script" "$SURFACE:$REMOTE_DIR/"
    fi

    # Launch in tmux session (detached, survives SSH disconnect)
    ssh "$SURFACE" "tmux kill-session -t nesting 2>/dev/null || true; \
        tmux new-session -d -s nesting \
        'source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py $remote_jobs $remote_results 2>&1 | tee ~/nesting.log'"

    info "Job launched in tmux session 'nesting'"
    echo ""
    echo "  Monitor:  $0 status"
    echo "  Results:  $0 results $results_name"
    echo "  Live log: ssh surface 'tail -f ~/nesting.log'"
}

cmd_status() {
    check_connection

    echo "--- Surface nesting status ---"
    echo ""

    # Check if tmux session exists
    if ssh "$SURFACE" "tmux has-session -t nesting 2>/dev/null"; then
        info "Nesting job is RUNNING"
        echo ""
        echo "Last 10 lines of output:"
        ssh "$SURFACE" "tail -10 ~/nesting.log 2>/dev/null" || true
    else
        warn "No active nesting session"
        echo ""
        echo "Last 5 lines of log:"
        ssh "$SURFACE" "tail -5 ~/nesting.log 2>/dev/null" || echo "  (no log file)"
    fi

    echo ""
    echo "Result files on Surface:"
    ssh "$SURFACE" "ls -lh ~/results_*.json 2>/dev/null" || echo "  (none yet)"
}

cmd_results() {
    local results_name="${1:-}"

    check_connection

    if [ -z "$results_name" ]; then
        echo "Available result files:"
        ssh "$SURFACE" "ls -lh ~/results_*.json 2>/dev/null" || echo "  (none)"
        echo ""
        echo "Usage: $0 results <filename.json>"
        return
    fi

    local remote_path="$REMOTE_DIR/$results_name"
    local local_path="$LOCAL_RESULTS/$results_name"

    info "Fetching $results_name → $local_path"
    scp "$SURFACE:$remote_path" "$local_path"

    # Show summary
    echo ""
    python3 -c "
import json
results = json.load(open('$local_path'))
print(f'  Jobs completed: {len(results)}')
for r in results:
    if 'error' in r:
        print(f'  {r[\"label\"]}: ERROR - {r[\"error\"]}')
    else:
        print(f'  {r[\"label\"]}: {r[\"efficiency\"]:.2f}%  {r[\"length_yards\"]:.4f}yd  {r[\"computation_time_s\"]:.0f}s')
"
}

cmd_test() {
    check_connection

    info "Running 60s test job..."

    # Create a quick test job from existing 480s jobs
    ssh "$SURFACE" "source ~/nester/bin/activate && python3 -c \"
import json
with open('$REMOTE_DIR/surface_jobs_480s.json') as f:
    jobs = json.load(f)
job = jobs[0].copy()
job['config']['time_limit_s'] = 60
# Clear split times if present
job['config'].pop('exploration_time', None)
job['config'].pop('compression_time', None)
job['label'] = 'test_60s'
with open('$REMOTE_DIR/test_60s.json', 'w') as f:
    json.dump([job], f)
print('Test job created')
\""

    info "Running nesting (should take ~70s)..."
    ssh "$SURFACE" "source ~/nester/bin/activate && python3 ~/surface_nesting_worker.py ~/test_60s.json ~/results_test.json"

    echo ""
    info "Test complete! Results:"
    ssh "$SURFACE" "cat ~/results_test.json" | python3 -m json.tool
}

cmd_ssh() {
    ssh "$SURFACE"
}

# --- Main dispatcher ---
case "${1:-help}" in
    submit)  shift; cmd_submit "$@" ;;
    status)  cmd_status ;;
    results) shift; cmd_results "$@" ;;
    test)    cmd_test ;;
    ssh)     cmd_ssh ;;
    *)
        echo "Surface Nesting Job Manager"
        echo ""
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  submit <jobs.json> [results.json]  Submit a nesting job"
        echo "  status                              Check job progress"
        echo "  results [filename.json]             Fetch results"
        echo "  test                                Run 60s test"
        echo "  ssh                                 Interactive SSH session"
        ;;
esac
