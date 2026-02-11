# Plan: Reorganize Claude Memory Files

## Current State

| Item | Location | Issue |
|------|----------|-------|
| Working directory | `/home/sarv/projects/garment-nester` | Wrong - should be MarkerMind |
| Session files | `~/.claude/projects/-home-sarv-projects-garment-nester/` | 16 sessions stored here |
| MarkerMind CLAUDE.md | `/home/sarv/projects/MarkerMind/CLAUDE.md` | Contains garment-nester content |
| garment-nester CLAUDE.md | `/home/sarv/projects/garment-nester/CLAUDE.md` | Nesting engine docs (correct) |

## Problem

1. All Claude sessions are under garment-nester project
2. MarkerMind CLAUDE.md references `src/nesting_engine/` which doesn't exist
3. The web app (frontend/backend) is in MarkerMind, not garment-nester

## Solution

### Step 1: Create proper MarkerMind CLAUDE.md

Replace MarkerMind's CLAUDE.md with content focused on:
- Web application architecture (frontend/backend)
- Quick start commands for MarkerMind
- Reference to APP_FLOW_ARCHITECTURE.md for full documentation
- Link to garment-nester for nesting algorithm details

### Step 2: Update file structure

```
/home/sarv/projects/MarkerMind/
├── CLAUDE.md                    # Web app focused (NEW)
├── REQUIREMENTS_CHECKLIST.md    # Keep as-is
├── docs/
│   └── APP_FLOW_ARCHITECTURE.md # Full architecture (DONE)
├── frontend/                    # Next.js app
└── backend/                     # FastAPI app

/home/sarv/projects/garment-nester/
├── CLAUDE.md                    # Keep - nesting engine docs
├── src/nesting_engine/          # Core nesting library
└── scripts/                     # GPU nesting, ILP solvers
```

### Step 3: Switch working directory

User should `cd /home/sarv/projects/MarkerMind` and start new Claude sessions from there.

## New MarkerMind CLAUDE.md Content

```markdown
# MarkerMind - Claude Code Guide

## Quick Start

### Start the Application
# Terminal 1: Backend
cd /home/sarv/projects/MarkerMind/backend
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000

# Terminal 2: Frontend
cd /home/sarv/projects/MarkerMind/frontend
npm run dev

# Access: http://localhost:3000

### Run Database Migrations
cd /home/sarv/projects/MarkerMind/backend
source venv/bin/activate
alembic upgrade head

## Architecture Overview

See [docs/APP_FLOW_ARCHITECTURE.md](docs/APP_FLOW_ARCHITECTURE.md) for complete documentation:
- Database models & relationships
- Frontend state management (Zustand)
- Backend services & API routes
- 6-step workflow with persistence
- Data flow diagrams

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `frontend/src/app/` | Next.js pages |
| `frontend/src/lib/` | API client, auth store |
| `backend/backend/models/` | SQLAlchemy ORM |
| `backend/backend/services/` | Business logic |
| `backend/backend/api/routes/` | FastAPI endpoints |
| `backend/alembic/versions/` | DB migrations |

## Development Commands

# Frontend
cd frontend && npm run dev          # Start dev server
cd frontend && npm run build        # Production build
cd frontend && npm run lint         # Lint check

# Backend
cd backend && uvicorn backend.main:app --reload  # Start API
cd backend && alembic upgrade head               # Run migrations
cd backend && alembic revision -m "message"      # Create migration

# Database
psql -U postgres -d markermind      # Connect to DB

## Current Gaps / TODOs

1. Multi-material Orders - Only processes first material per nesting job
2. Export - No production export format
3. Undo/History - No audit trail
4. Batch Nesting - Can't run multiple fabrics in parallel

## Related Projects

For nesting algorithm details (GPU FFT, ILP solver), see:
- `/home/sarv/projects/garment-nester/CLAUDE.md`
- `/home/sarv/projects/garment-nester/docs/gpu_nesting.md`
- `/home/sarv/projects/garment-nester/docs/cutplan_optimizer.md`

The nesting runners in `backend/backend/services/` import from garment-nester.
```

## Implementation Steps

1. [ ] Backup existing MarkerMind CLAUDE.md
2. [ ] Write new MarkerMind CLAUDE.md (web app focused)
3. [ ] Verify garment-nester CLAUDE.md is correct (nesting engine)
4. [ ] User switches to MarkerMind as working directory
5. [ ] Future sessions will be stored under MarkerMind project
