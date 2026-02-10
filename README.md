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
