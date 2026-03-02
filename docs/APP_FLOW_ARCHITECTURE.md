# MarkerMind - Application Flow & Architecture

## Overview

MarkerMind is a full-stack web application for garment manufacturing that automates cutting pattern optimization. It uses GPU-accelerated nesting and ILP optimization to generate optimal cutting plans.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| State Management | Zustand (persisted to localStorage) |
| Backend | FastAPI, Python 3.11 |
| Database | PostgreSQL |
| GPU Nesting | CuPy, FFT Convolution |
| ILP Solver | PuLP/Gurobi |

---

## Database Models & Relationships

```
Customer (Tenant)
├── Users
├── Fabrics
├── Patterns
│   └── PatternFabricMappings
├── Orders
│   ├── OrderLines
│   │   └── SizeQuantities
│   ├── NestingJobs
│   │   └── NestingJobResults
│   └── Cutplans
│       └── CutplanMarkers
└── CostConfigs

MarkerBank (pattern + fabric + ratio → efficiency)
```

### Key Models

| Model | File | Purpose |
|-------|------|---------|
| `Order` | `models/order.py` | Customer orders with lines and quantities |
| `Pattern` | `models/pattern.py` | DXF/RUL files and parsed metadata |
| `NestingJob` | `models/nesting.py` | GPU nesting job tracking |
| `MarkerBank` | `models/marker.py` | Evaluated markers for ILP selection |
| `Cutplan` | `models/cutplan.py` | Final cutting plans with cost breakdown |

---

## Frontend State Management

### Auth Store (`lib/auth-store.ts`)

```typescript
useAuthStore = create(persist({
  user: User | null,
  isAuthenticated: boolean,
  login(), logout(), checkAuth()
}))
```

- **Persistence:** localStorage (key: `auth-storage`)
- **Token:** localStorage (key: `token`)

### API Client (`lib/api.ts`)

- Singleton class with typed methods for all endpoints
- Auto-adds `Authorization: Bearer {token}` header
- Handles 401 → logout redirect

---

## Backend Services

| Service | File | Purpose |
|---------|------|---------|
| `AuthService` | `services/auth_service.py` | JWT tokens, password hashing |
| `NestingService` | `services/nesting_service.py` | GPU job management, preview cache |
| `CutplanService` | `services/cutplan_service.py` | ILP optimization, cost calculation |
| `PatternService` | `services/pattern_service.py` | DXF parsing, piece extraction |

---

## 6-Step Workflow

### Step 1: Order Creation
**Status:** `draft` → `pending_pattern`

| Action | Persistence |
|--------|-------------|
| Create order | `Order` record |
| Add lines | `OrderLine`, `SizeQuantity` records |
| Import batch | Auto-creates `Fabric` records |

### Step 2: Link Pattern
**Status:** `pending_pattern` → `pending_nesting`

| Action | Persistence |
|--------|-------------|
| Upload DXF/RUL | Files saved, `Pattern` record |
| Parse pattern | `pattern.parse_metadata`, sizes, materials |
| Map materials | `PatternFabricMapping` records |
| Link to order | `Order.pattern_id` set |

### Step 3: Configure Nesting
**Status:** `pending_nesting`

| Config | Default | Stored In |
|--------|---------|-----------|
| Fabric width | From Fabric record | `NestingJob.fabric_width_inches` |
| Max bundles | 6 | `NestingJob.max_bundle_count` |
| Top N | 10 | `NestingJob.top_n_results` |
| Full coverage | false | `NestingJob.full_coverage` |

### Step 4: Run GPU Nesting
**Status:** `pending_nesting` → `nesting_in_progress` → `pending_cutplan`

| Action | Persistence |
|--------|-------------|
| Create job | `NestingJob` record (status: pending) |
| Run background | `NestingJob.status` → running |
| Evaluate ratios | GPU FFT convolution |
| Store previews | In-memory cache (`_preview_cache`) |
| Save results | `NestingJobResult` records |
| Update bank | `MarkerBank` records (deduped) |
| Complete | `Order.status` → pending_cutplan |

**Real-time Updates:**
- WebSocket `/nesting/jobs/{id}/stream` (1s polling)
- Preview endpoint `/nesting/jobs/{id}/preview` (base64 PNG)

### Step 5: Optimize Cutplan
**Status:** `pending_cutplan` → `cutplan_ready`

| Action | Persistence |
|--------|-------------|
| Get demand | Aggregate `SizeQuantity` by size |
| Get markers | Query `MarkerBank` for pattern/fabric |
| Run ILP | Multiple strategies (max_eff, balanced, min_markers) |
| Calculate costs | Using `CostConfig` |
| Save plans | `Cutplan`, `CutplanMarker` records |

**Cost Components:**
- Fabric cost = yards × cost_per_yard
- Spreading cost = yards × spreading_rate
- Cutting cost = cuts × cost_per_cut
- Prep cost = markers × prep_cost_per_marker

### Step 6: Approve Cutplan
**Status:** `cutplan_ready` → `approved`

| Action | Persistence |
|--------|-------------|
| User approves | `Cutplan.status` → approved |
| Order complete | `Order.status` → approved |

---

## API Endpoints Summary

### Auth (`/auth`)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/auth/register` | POST | Create user + customer |
| `/auth/login` | POST | Get JWT token |
| `/auth/logout` | POST | Invalidate token |
| `/auth/me` | GET | Current user |

### Orders (`/orders`)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/orders` | GET/POST | List/create orders |
| `/orders/{id}` | GET/PUT/DELETE | CRUD order |
| `/orders/import-batch` | POST | Batch import Excel |

### Patterns (`/patterns`)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/patterns/upload` | POST | Upload DXF + RUL |
| `/patterns/{id}/parse` | POST | Parse DXF |
| `/patterns/{id}/fabric-mapping` | POST | Map materials |
| `/patterns/{id}/pieces` | GET | Get piece list |

### Nesting (`/nesting`)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/nesting/jobs` | POST | Create GPU job |
| `/nesting/jobs/{id}` | GET | Get job status |
| `/nesting/jobs/{id}/stream` | WS | Real-time progress |
| `/nesting/jobs/{id}/preview` | GET | Current preview |
| `/nesting/markers` | GET | List marker bank |

### Cutplans (`/cutplans`)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/cutplans/optimize` | POST | Run ILP optimization |
| `/cutplans/{id}` | GET | Get cutplan |
| `/cutplans/{id}/approve` | POST | Approve for production |
| `/cutplans/{id}/cost-analysis` | GET | Cost breakdown |

---

## Data Flow Diagrams

### Authentication
```
Frontend                    Backend                Database
   |-- POST /auth/login ------>|                      |
   |                           |-- Check email ------>|
   |                           |-- Create Session --->|
   |<---- { token, user } -----|                      |
   |-- Store in localStorage   |                      |
```

### GPU Nesting
```
Frontend              Backend                 GPU              Database
   |-- Create Job ------->|                    |                  |
   |                      |-- Save Job ----------------->| NestingJob
   |                      |-- Background Task  |                  |
   |                      |-- run_gpu_nesting ->|                 |
   |<-- WS: progress -----|<---- callback -----|                  |
   |<-- WS: preview ------|<---- preview ------|                  |
   |                      |                    |-- Evaluate       |
   |                      |                    |-- FFT convolve   |
   |<-- WS: complete -----|<---- results -----|                   |
   |                      |-- Save Results --------->| NestingJobResult
   |                      |-- Update Bank ---------->| MarkerBank
```

### ILP Optimization
```
Frontend              Backend              Solver           Database
   |-- Optimize --------->|                  |                |
   |                      |-- Get Demand ---------->| OrderLine
   |                      |-- Get Markers --------->| MarkerBank
   |                      |-- Run ILP ------>|                |
   |                      |<-- Solution -----|                |
   |                      |-- Calc Costs --->|                |
   |                      |-- Save Plan ----------->| Cutplan
   |<-- Cutplan Options --|                  |                |
```

---

## Key File Locations

### Backend
```
backend/backend/
├── models/           # SQLAlchemy ORM models
├── schemas/          # Pydantic request/response schemas
├── api/routes/       # FastAPI route handlers
├── services/         # Business logic
│   ├── nesting_service.py
│   ├── cutplan_service.py
│   ├── gpu_nesting_runner.py
│   └── ilp_solver_runner.py
└── database.py       # DB connection
```

### Frontend
```
frontend/src/
├── app/              # Next.js pages
│   ├── orders/       # Order pages
│   └── login/        # Auth pages
├── lib/
│   ├── api.ts        # API client
│   └── auth-store.ts # Zustand store
└── components/       # UI components
```

---

## Persistence Summary

| Data | Storage | When |
|------|---------|------|
| Auth token | localStorage | Login |
| User session | PostgreSQL `sessions` | Login |
| Orders | PostgreSQL `orders` | Step 1 |
| Pattern files | Filesystem | Step 2 |
| Pattern metadata | PostgreSQL `patterns` | Step 2 |
| GPU previews | In-memory dict | Step 4 (transient) |
| Nesting results | PostgreSQL `nesting_job_results` | Step 4 |
| Marker bank | PostgreSQL `marker_bank` | Step 4 |
| Cutplans | PostgreSQL `cutplans` | Step 5 |

---

## Current Gaps / TODOs

1. **Celery Integration** - Background tasks use FastAPI `BackgroundTasks`, not Celery
2. **Multi-material Orders** - Currently processes first material only per nesting job
3. **Export** - No production export format yet
4. **Undo/History** - No audit trail or rollback capability
5. **Batch Nesting** - Can't run nesting for multiple fabrics in parallel

---

## Configuration

### Environment Variables
```bash
DATABASE_URL=postgresql://user:pass@localhost:5432/markermind
SECRET_KEY=your-secret-key
UPLOAD_DIR=/path/to/uploads
```

### Cost Defaults (CostConfig)
```python
fabric_cost_per_yard = 5.0
spreading_cost_per_yard = 0.10
cutting_cost_per_inch = 0.05
prep_cost_per_marker = 2.0
max_ply_height = 100
```
