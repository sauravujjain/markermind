# MarkerMind — Production Deployment Guide

## Architecture Overview

MarkerMind runs as a multi-tenant SaaS on Google Cloud Platform (GCP). Each customer gets a subdomain (`acme.markermind.com`) that routes to a shared application with data isolation at the database level.

```
Customer browser → acme.markermind.com
    │
    ▼
Global External Application Load Balancer
    ├── Wildcard TLS cert (*.markermind.com)
    ├── Cloud CDN (static assets)
    └── Cloud Armor (DDoS protection)
    │
    ▼
Cloud Run (CPU) — Backend API + Frontend
    ├── Middleware: Host header → customer_id
    ├── FastAPI backend (Python 3.11)
    ├── Next.js frontend (SSR)
    │
    ├──► Cloud Run (GPU, L4) — Nesting Worker
    │       Scales 0 → N based on job queue
    │       Billed per second of GPU time
    │       ~5 second cold start
    │
    ├──► Cloud SQL (PostgreSQL) — All customer data
    │       Separate schema per customer
    │
    └──► Cloud Storage (GCS) — Pattern files, DXF exports
```

---

## GCP Services Used

All services are GCP-native. No third-party vendors required.

| Component | GCP Service | Purpose |
|-----------|------------|---------|
| Frontend + Backend API | Cloud Run (CPU) | Serves HTTP, auto-scales per request |
| GPU Nesting Worker | Cloud Run (GPU, L4) | Runs CuPy/FFT nesting, scales to zero |
| Database | Cloud SQL (PostgreSQL) | Customer data, cutplans, marker bank |
| File Storage | Cloud Storage (GCS) | Pattern DXF/RUL files, exports |
| Load Balancer | Global External Application LB | Wildcard subdomain routing, TLS termination |
| DNS | Cloud DNS | `*.markermind.com` wildcard record |
| TLS Certificates | Google-managed or custom wildcard | HTTPS for all subdomains |
| Secrets | Secret Manager | DB credentials, API keys |
| Monitoring | Cloud Monitoring + Logging | Metrics, alerts, centralized logs |

---

## GPU Nesting: Cloud Run with L4 GPU

### Why Cloud Run GPU

The GPU nesting workload is bursty — customers submit orders in batches during work hours, with zero activity at night. Cloud Run GPU provides:

- **Scale to zero**: No GPU cost when nobody is nesting
- **Per-second billing**: Only pay for actual compute time
- **~5 second cold start**: GPU drivers pre-installed, fast spin-up
- **Auto-scaling**: Handles burst of 100 orders by spinning up multiple GPU instances
- **No infrastructure management**: No CUDA driver updates, no VM patching

### Specifications

| Spec | Value |
|------|-------|
| GPU | NVIDIA L4 (24 GB VRAM) |
| CUDA | 12.2 (pre-installed driver 535.216.03) |
| Min resources per instance | 4 vCPU + 16 GiB RAM |
| Max instances per service | 1000 |
| Cold start | ~5 seconds |
| Scale to zero | Yes |

### Pricing (Verified Feb 2026)

| Resource | Rate | Source |
|----------|------|--------|
| L4 GPU | $0.000187/sec (~$0.67/hr) | [Cloud Run pricing](https://cloud.google.com/run/pricing) |
| vCPU (during GPU use) | $0.000024/vCPU-sec | [Cloud Run pricing](https://cloud.google.com/run/pricing) |
| Memory (during GPU use) | $0.0000025/GiB-sec | [Cloud Run pricing](https://cloud.google.com/run/pricing) |

**Cost per nesting job** (5 min average, 4 vCPU + 16 GiB):

| Component | Calculation | Cost |
|-----------|------------|------|
| GPU | 300s × $0.000187 | $0.056 |
| CPU | 300s × 4 × $0.000024 | $0.029 |
| Memory | 300s × 16 × $0.0000025 | $0.012 |
| **Total per nest** | | **~$0.10** |

### Container Image

The nesting worker runs as a Docker container with CuPy and CUDA:

```dockerfile
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y python3 python3-pip
RUN pip3 install cupy-cuda12x scipy numpy pillow fastapi uvicorn

COPY backend/ /app/backend/
COPY nesting_engine/ /app/nesting_engine/

CMD ["uvicorn", "app.backend.gpu_worker:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Deployment

```bash
# Build and push container
gcloud builds submit --tag gcr.io/PROJECT/markermind-gpu-worker

# Deploy to Cloud Run with GPU
gcloud run deploy markermind-gpu-worker \
  --image gcr.io/PROJECT/markermind-gpu-worker \
  --cpu 4 \
  --memory 16Gi \
  --gpu 1 \
  --gpu-type nvidia-l4 \
  --no-cpu-throttling \
  --max-instances 10 \
  --min-instances 0 \
  --region us-central1 \
  --execution-environment gen2
```

### GPU Quota

Projects are automatically granted 3 GPU instances on first deployment. Request a quota increase for higher concurrency.

### Why Not Modal.com (Decision: Feb 2026)

Modal.com was evaluated as an alternative GPU provider. Per-job costs are ~12% lower ($0.085 vs $0.097 on L4) due to cheaper CPU pricing, and Modal also offers T4 GPUs (~$0.068/job). However, the savings are ~$80/month at 100 nests/day — not enough to justify a second vendor.

**Decision: GCP all-in-one.** Rationale:
- Single bill, single console, single set of IAM roles, single place to debug
- No cross-vendor network hop (latency + failure point)
- One monitoring/logging system (Cloud Logging covers everything)
- At $1,000-1,500/month target pricing, 76% margin on GCP is already excellent
- Modal is a younger, VC-funded company — GCP has stronger longevity guarantees

**Contingency**: If Cloud Run GPU has regional availability issues or CuPy compatibility problems, Modal can be adopted as a fallback. The GPU worker sits behind a clean API boundary, making the swap straightforward.

---

## Database: Cloud SQL PostgreSQL

### Multi-Tenant Schema Isolation

Each customer gets a separate PostgreSQL schema within a shared Cloud SQL instance. This provides:

- Data isolation without managing multiple database instances
- Per-customer backup/restore via `pg_dump` of individual schemas
- Shared connection pool and instance resources
- Easy customer provisioning (create schema + seed tables)

```
Cloud SQL Instance
├── Schema: shared          ← customers table, global configs
├── Schema: acme            ← acme's orders, patterns, cutplans
├── Schema: boke            ← boke's orders, patterns, cutplans
└── Schema: valai           ← valai's orders, patterns, cutplans
```

### Instance Sizing

| Customers | Instance Type | vCPU | RAM | Monthly Cost |
|-----------|-------------|------|-----|-------------|
| 1-5 (dev/early) | db-f1-micro | 0.25 | 0.6 GB | ~$9/mo |
| 5-20 | db-g1-small | 0.5 | 1.7 GB | ~$26/mo |
| 20-50 | db-custom-1-3840 | 1 | 3.75 GB | ~$35/mo |
| 50+ | db-custom-2-7680 | 2 | 7.5 GB | ~$70/mo |

Add SSD storage at ~$0.17/GB/month. Typical customer uses 1-5 GB, so 100 GB total ($17/mo) covers 20-50 customers.

> Note: Shared-core instances (micro, small) are not covered by the Cloud SQL SLA. Upgrade to dedicated cores before promising uptime guarantees.

### Pricing Source

[Cloud SQL pricing](https://cloud.google.com/sql/pricing)

---

## Subdomain Routing

### Architecture

```
*.markermind.com
    │
    ▼ (Wildcard DNS → Load Balancer IP)
Global External Application Load Balancer
    ├── Wildcard TLS termination
    ├── URL mask: *.markermind.com → Cloud Run service
    └── Cloud CDN for static assets
    │
    ▼
Cloud Run (Backend)
    └── Middleware extracts Host header → maps to customer schema
```

### Why a Load Balancer (Not Direct Cloud Run Domain Mapping)

Cloud Run's built-in custom domain mapping does not support wildcard domains. For `*.markermind.com` routing, you need a Global External Application Load Balancer with URL masks. This also gives you:

- Wildcard TLS certificate support
- Cloud CDN integration
- Cloud Armor DDoS protection
- Multi-region failover capability

### DNS Setup

```
# In Cloud DNS (or Cloudflare, etc.)
*.markermind.com    A     <load-balancer-IP>
markermind.com      A     <load-balancer-IP>
```

### Application Middleware

```python
# backend/backend/middleware/tenant.py
def resolve_tenant(request):
    host = request.headers.get("host", "")
    subdomain = host.split(".")[0] if "." in host else None

    if subdomain in ("www", "admin", "api"):
        return None  # Not a tenant subdomain

    customer = db.query(Customer).filter(
        Customer.subdomain == subdomain,
        Customer.is_active == True,
    ).first()

    if not customer:
        raise HTTPException(404, "Unknown tenant")

    # Set schema search path for this request
    db.execute(f"SET search_path TO {customer.schema_name}, shared")
    return customer
```

### Customer Provisioning

Adding a new customer requires zero infrastructure changes:

1. Insert row in `shared.customers` table (name, subdomain, schema_name)
2. Create PostgreSQL schema with tables (automated migration script)
3. Seed default cost configs and admin user
4. Send welcome email

No DNS changes needed — wildcard record already covers all subdomains.

### Security: Cookie Isolation

Register `markermind.com` on the [Public Suffix List](https://publicsuffix.org/) so browsers treat each tenant subdomain as an independent site. This prevents `acme.markermind.com` cookies from being readable by `boke.markermind.com`.

---

## File Storage: Cloud Storage (GCS)

Pattern files (DXF, RUL), generated DXF exports, and SVG previews are stored in GCS.

### Bucket Structure

```
gs://markermind-files/
├── patterns/
│   ├── acme/           ← per-customer prefix
│   │   ├── pattern-uuid/
│   │   │   ├── original.dxf
│   │   │   ├── original.rul
│   │   │   └── metadata.json
│   └── boke/
├── exports/
│   ├── acme/
│   │   └── cutplan-uuid/
│   │       ├── markers.zip
│   │       └── cutplan.xlsx
└── previews/
    └── marker-uuid.svg
```

### Pricing

| Resource | Rate |
|----------|------|
| Standard storage | $0.020/GB/month (us region) |
| Operations (Class A) | $0.05/10,000 |
| Operations (Class B) | $0.004/10,000 |
| Egress (within GCP) | Free |

Typical usage: 5 GB per customer → $0.10/mo per customer.

[Cloud Storage pricing](https://cloud.google.com/storage/pricing)

---

## Cost Summary

### Fixed Infrastructure Costs (Shared Across All Customers)

These costs are incurred regardless of usage — they run 24/7:

| Component | Monthly Cost |
|-----------|-------------|
| Cloud SQL instance (db-f1-micro → db-g1-small) | $9-26 |
| Load Balancer (forwarding rule) | $18 |
| Cloud DNS zone | $0.20 |
| Cloud Run CPU (min instances = 1) | $30-50 |
| **Fixed total** | **~$60-95/mo** |

Start with db-f1-micro ($9) for first 1-3 customers, upgrade to db-g1-small ($26) at 5+.

### Per-Job GPU Cost (Verified Feb 2026)

Each nesting job (5 min avg, 4 vCPU, 16 GiB on L4):

| Component | Calculation | Cost |
|-----------|------------|------|
| GPU | 300s × $0.000187 | $0.056 |
| CPU | 300s × 4 × $0.000024 | $0.029 |
| Memory | 300s × 16 × $0.0000025 | $0.012 |
| **Total per nest** | | **~$0.10** |

### Customer Usage Scenarios

All scenarios assume 22 working days/month:

| Scenario | Nests/day | Jobs/month | GPU cost | Infra share (5 customers) | **Total COGS** |
|----------|-----------|-----------|----------|--------------------------|---------------|
| Light | 30 | 660 | $66 | $15 | **~$80** |
| Medium | 100 | 2,200 | $220 | $15 | **~$235** |
| Heavy | 150 | 3,300 | $330 | $15 | **~$345** |

### Single-Customer Phase (Early Launch)

With only 1 customer bearing all fixed infra:

| Nests/day | GPU cost | Fixed infra | **Total COGS** |
|-----------|---------|------------|---------------|
| 30 | $66 | $75 | **~$140** |
| 100 | $220 | $75 | **~$295** |
| 150 | $330 | $75 | **~$405** |

### Scaling: Cost Per Customer by Volume (at 100 nests/day avg)

| Customers | Fixed infra/customer | GPU | DB upgrade | **Total/customer** |
|-----------|---------------------|-----|-----------|-------------------|
| 1 | $75 | $220 | — | **$295** |
| 5 | $15 | $220 | — | **$235** |
| 10 | $8 | $220 | $3 | **$231** |
| 20 | $5 | $220 | $4 | **$229** |

GPU dominates the cost. Infrastructure overhead becomes negligible beyond 5 customers.

---

## Pricing Strategy

**Target: $1,000-1,500/month per customer.** At this price point with GCP all-in-one:

| Customer type | Nests/day | COGS (at 5 customers) | Price | **Margin** |
|---------------|-----------|----------------------|-------|-----------|
| Light factory | 30 | ~$80 | $1,000 | **92%** |
| Medium factory | 100 | ~$235 | $1,250 | **81%** |
| Heavy factory | 150 | ~$345 | $1,500 | **77%** |

### Suggested Pricing Tiers

| Tier | Nests/month | Price/month | COGS | Margin |
|------|------------|------------|------|--------|
| Starter | Up to 1,000 | $1,000 | ~$100 | 90% |
| Standard | Up to 3,000 | $1,250 | ~$300 | 76% |
| Pro | Up to 5,000 | $1,500 | ~$500 | 67% |
| Enterprise | Unlimited, SLA, dedicated DB | $2,500+ | Variable | 60%+ |

Margins are healthy across all tiers. Even the heaviest user at Pro tier yields 67%.
The Enterprise tier covers customers needing data isolation, guaranteed GPU concurrency, or custom SLAs.

---

## Deployment Workflow

### Initial Setup (One-Time)

```bash
# 1. Create GCP project
gcloud projects create markermind-prod

# 2. Enable required APIs
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  compute.googleapis.com \
  dns.googleapis.com \
  secretmanager.googleapis.com

# 3. Create Cloud SQL instance
gcloud sql instances create markermind-db \
  --database-version=POSTGRES_15 \
  --tier=db-g1-small \
  --region=us-central1

# 4. Create GCS bucket
gsutil mb -l us-central1 gs://markermind-files

# 5. Deploy backend + frontend (CPU)
gcloud run deploy markermind-app \
  --source . \
  --cpu 2 --memory 2Gi \
  --min-instances 1 \
  --max-instances 10 \
  --region us-central1

# 6. Deploy GPU worker
gcloud run deploy markermind-gpu-worker \
  --image gcr.io/markermind-prod/gpu-worker \
  --cpu 4 --memory 16Gi \
  --gpu 1 --gpu-type nvidia-l4 \
  --min-instances 0 \
  --max-instances 10 \
  --no-cpu-throttling \
  --region us-central1 \
  --execution-environment gen2

# 7. Set up load balancer with wildcard cert
# (See GCP docs: https://cloud.google.com/run/docs/mapping-custom-domains)

# 8. Configure DNS
gcloud dns record-sets create "*.markermind.com" \
  --zone=markermind-zone \
  --type=A \
  --rrdatas=<LOAD_BALANCER_IP>
```

### Adding a New Customer

```bash
# No infrastructure changes needed. Just an API call:
curl -X POST https://admin.markermind.com/api/customers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{
    "name": "New Factory",
    "subdomain": "newfactory",
    "admin_email": "boss@factory.com",
    "plan": "standard"
  }'

# This creates:
# 1. Customer record in shared.customers
# 2. PostgreSQL schema "newfactory" with all tables
# 3. Default cost configs
# 4. Admin user account
# 5. Welcome email
#
# Customer immediately accesses: newfactory.markermind.com
```

### Deploying Updates

```bash
# Build and deploy — all customers get the update simultaneously
gcloud run deploy markermind-app --source .
gcloud run deploy markermind-gpu-worker --image gcr.io/markermind-prod/gpu-worker

# Run database migrations (applies to all schemas)
python manage.py migrate --all-schemas
```

---

## Monitoring and Alerts

### Key Metrics to Track

| Metric | Alert Threshold | Why |
|--------|----------------|-----|
| GPU worker cold starts | >10s p95 | Customer experience |
| Nesting job duration | >15 min | Possible hung job |
| Cloud SQL CPU | >80% sustained | Time to upgrade instance |
| Cloud SQL connections | >80% of max | Connection pool exhaustion |
| Error rate (5xx) | >1% of requests | Application issues |
| GPU queue depth | >20 pending jobs | Need more GPU concurrency |

### Logging

All Cloud Run logs flow to Cloud Logging automatically. Set up log-based alerts for:
- GPU out-of-memory errors
- Database connection failures
- Authentication failures (brute force detection)

---

## Security Checklist

- [ ] Wildcard TLS certificate on load balancer
- [ ] Cloud Armor WAF rules (OWASP top 10)
- [ ] Register `markermind.com` on Public Suffix List (cookie isolation)
- [ ] Cloud SQL: private IP only (no public access)
- [ ] Cloud SQL: SSL connections required
- [ ] GCS: uniform bucket-level access (no per-object ACLs)
- [ ] Secret Manager for all credentials (no env vars)
- [ ] IAM: least privilege for Cloud Run service accounts
- [ ] VPC connector between Cloud Run and Cloud SQL
- [ ] Automated database backups (Cloud SQL built-in)

---

## Migration Path: Shared → Dedicated

If a customer requires full isolation (compliance, data residency, guaranteed GPU):

1. Create dedicated Cloud SQL instance for the customer
2. `pg_dump` their schema from shared instance → `pg_restore` to dedicated
3. Create dedicated GCS bucket
4. Deploy dedicated Cloud Run services
5. Update DNS: `customer.markermind.com` → dedicated load balancer
6. Delete schema from shared instance

This can be done with zero downtime using a maintenance window.

---

## Sources

- [Cloud Run GPU — General Availability](https://cloud.google.com/blog/products/serverless/cloud-run-gpus-are-now-generally-available)
- [Cloud Run GPU Configuration](https://docs.google.com/run/docs/configuring/services/gpu)
- [Cloud Run Pricing](https://cloud.google.com/run/pricing)
- [Cloud SQL Pricing](https://cloud.google.com/sql/pricing)
- [Cloud Storage Pricing](https://cloud.google.com/storage/pricing)
- [GPU Pricing (Compute Engine reference)](https://cloud.google.com/compute/gpus-pricing)
- [Cloud Run Custom Domain Mapping](https://cloud.google.com/run/docs/mapping-custom-domains)
- [NVIDIA L4 GPU Price Comparison (Modal blog)](https://modal.com/blog/nvidia-l4-price-article)
- [Top Serverless GPU Clouds 2026 (RunPod)](https://www.runpod.io/articles/guides/top-serverless-gpu-clouds)
- [Cloud Run GPU + CUDA Demo (GitHub)](https://github.com/GoogleCloudPlatform/cloudrun-gpus-opencv-cuda-demo)
- [Multi-Tenant Wildcard TLS](https://www.skeptrune.com/posts/wildcard-tls-for-multi-tenant-systems/)
