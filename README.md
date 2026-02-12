# MarkerMind - Cutting Optimization Platform

AI-powered cutting optimization for garment manufacturing, built on top of the garment-nester engine.

## Architecture

```
MarkerMind/
├── backend/          # FastAPI backend
│   ├── backend/
│   │   ├── api/      # REST endpoints
│   │   ├── models/   # SQLAlchemy models
│   │   ├── schemas/  # Pydantic schemas
│   │   ├── services/ # Business logic
│   │   └── workers/  # Celery tasks
│   └── alembic/      # DB migrations
├── frontend/         # Next.js frontend
│   └── src/
│       ├── app/      # Pages (App Router)
│       ├── components/
│       ├── hooks/
│       └── lib/      # API client, stores
├── nesting_engine/   # Core nesting library (from garment-nester)
├── scripts/          # GPU nesting, ILP solvers (from garment-nester)
└── docker-compose.yml
```

## Quick Start

### 1. Start Infrastructure

```bash
cd /home/sarv/projects/MarkerMind
docker-compose up -d
```

This starts PostgreSQL and Redis.

### 2. Set Up Backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp ../.env.example .env

# Run database migrations
alembic upgrade head

# Start backend
uvicorn backend.main:app --reload --port 8000
```

### 3. Set Up Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev
```

### 4. Access Application

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API Docs: http://localhost:8000/docs

## Workflow

1. **Order Entry** - Upload Excel/CSV with order quantities
2. **Pattern Upload** - Upload DXF/AAMA pattern files
3. **GPU Nesting** - Run GPU-accelerated marker evaluation
4. **Cutplan ILP** - Generate optimized cutting plans
5. **Export** - Download cutting dockets and reports

## API Endpoints

### Auth
- `POST /api/auth/register` - Register user
- `POST /api/auth/login` - Login
- `POST /api/auth/logout` - Logout
- `GET /api/auth/me` - Current user

### Orders
- `GET/POST /api/orders` - List/Create orders
- `GET/PUT/DELETE /api/orders/{id}` - Order CRUD
- `POST /api/orders/import` - Excel/CSV import

### Patterns
- `POST /api/patterns/upload` - Upload DXF/RUL
- `POST /api/patterns/{id}/parse` - Parse pattern
- `POST /api/patterns/{id}/fabric-mapping` - Map fabrics

### Nesting
- `POST /api/nesting/jobs` - Submit GPU job
- `GET /api/nesting/jobs/{id}` - Job status
- `WS /api/nesting/jobs/{id}/stream` - Real-time progress

### Cutplans
- `POST /api/cutplans/optimize` - Run ILP
- `POST /api/cutplans/{id}/approve` - Approve plan
- `GET /api/cutplans/{id}/cost-analysis` - Cost breakdown

### Exports
- `GET /api/export/cutplan/{id}/docket` - Cutting docket
- `GET /api/export/cutplan/{id}/csv` - CSV export

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS, shadcn/ui |
| State | TanStack Query, Zustand |
| Backend | FastAPI, SQLAlchemy, Pydantic |
| Database | PostgreSQL |
| Queue | Celery + Redis |
| Nesting | CuPy (GPU), Spyrrow (CPU) |

## Environment Variables

```env
# Database
DATABASE_URL=postgresql://markermind:markermind_dev@localhost:5432/markermind

# Redis
REDIS_URL=redis://localhost:6379/0

# JWT
JWT_SECRET_KEY=your-secret-key-change-in-production
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=60

# App
DEBUG=true
CORS_ORIGINS=http://localhost:3000

# File Upload
UPLOAD_DIR=./uploads
MAX_UPLOAD_SIZE_MB=100
```

## Cross-Project Dependency: garment-nester

MarkerMind depends on the `garment-nester` project at `/home/sarv/projects/garment-nester`.
The backend adds it to `sys.path` at runtime to import `nesting_engine.*` modules.

**Why this matters:** Pure Python modules (core, io, engine wrappers) are imported via
`sys.path`, but **compiled native packages** like `spyrrow` must be `pip install`-ed
directly into the backend venv. They can't be found via `sys.path` manipulation.

### Nesting Dependencies (in backend venv)

| Package | Type | Required For | Notes |
|---------|------|--------------|-------|
| `spyrrow>=0.8` | **Compiled (Rust)** | CPU nesting (SpyrrowEngine) | Must be pip-installed in backend venv |
| `ezdxf>=1.0.0` | Python | DXF parsing and export | Used by AAMA parser + marker export |
| `shapely>=2.0.0` | Compiled (C) | Geometry operations | Used by AAMA parser |
| `numpy>=1.24.0` | Compiled (C) | GPU nesting rasterization | |
| `pillow>=9.0.0` | Compiled (C) | GPU nesting rasterization | |
| `scipy>=1.10.0` | Compiled (C) | GPU FFT nesting | |
| `cupy-cuda12x` | Compiled (CUDA) | GPU acceleration | Optional, requires NVIDIA GPU + CUDA |

### Pre-Startup Dependency Check

Run this before starting the backend to verify all nesting dependencies are available:

```bash
cd backend
source venv/bin/activate

python -c "
import sys
ok = True
for pkg in ['spyrrow', 'ezdxf', 'shapely', 'numpy', 'PIL', 'scipy']:
    try:
        mod = __import__(pkg)
        ver = getattr(mod, '__version__', '?')
        print(f'  OK  {pkg} ({ver})')
    except ImportError:
        print(f'  MISSING  {pkg}')
        ok = True if pkg == 'cupy' else False  # cupy is optional

# Check garment-nester path
gn = '$(realpath ../../../garment-nester 2>/dev/null || echo /home/sarv/projects/garment-nester)'
sys.path.insert(0, gn)
try:
    from nesting_engine.core.piece import Piece
    print(f'  OK  nesting_engine (via {gn})')
except ImportError:
    print(f'  MISSING  nesting_engine at {gn}')
    ok = False

print()
print('All dependencies OK!' if ok else 'MISSING DEPENDENCIES — run: pip install -r requirements.txt')
"
```

If `spyrrow` is missing:
```bash
pip install spyrrow>=0.8
```

## Full Startup Sequence

```bash
# 1. Start infrastructure (PostgreSQL + Redis)
cd /home/sarv/projects/MarkerMind
docker-compose up -d

# 2. Verify containers are running
docker ps | grep -E "postgres|redis"

# 3. Start backend
cd backend
source venv/bin/activate
uvicorn backend.main:app --reload --port 8000

# 4. Start frontend
cd frontend
npm run dev
```

## Development

### Run Celery Worker (for background jobs)

```bash
cd backend
celery -A backend.workers worker --loglevel=info
```

### Run Tests

```bash
# Backend
cd backend
pytest

# Frontend
cd frontend
npm test
```

## License

Proprietary - All rights reserved.
