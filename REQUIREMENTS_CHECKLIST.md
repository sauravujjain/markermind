# MarkerMind - Requirements Checklist

This document tracks current implementation status and future requirements for the MarkerMind application.

---

## Database Robustness & Data Integrity

### Implemented
- [x] UUID primary keys for all entities (Orders, Patterns, Fabrics, etc.)
- [x] Auto-increment naming for duplicate uploads (e.g., "Order 1", "Order 1 (2)")
- [x] No data overwriting - each upload creates a new record
- [x] Timestamps on all records (`created_at`, `updated_at`)

### Future Requirements
- [ ] **Soft Delete** - Add `is_deleted` boolean flag instead of hard deletes
  - Preserves audit trail
  - Allows data recovery
  - Required for: Orders, Patterns, Fabrics, Cutplans

- [ ] **Audit Log Table** - Track all changes
  - Fields: `entity_type`, `entity_id`, `action`, `old_value`, `new_value`, `user_id`, `timestamp`
  - Required for compliance and debugging

- [ ] **User Confirmation for Duplicates** - Frontend dialog
  - "This order already exists. Would you like to: [Update Existing] [Create New Copy] [Cancel]"
  - Gives user control over duplicate handling

- [ ] **Document Versioning** - Version field for revisable entities
  - `version` integer field
  - `parent_id` to link versions
  - Useful for: Orders, Cutplans, Patterns

- [ ] **Idempotency Keys** - For API retry safety
  - Client sends unique key per request
  - Server deduplicates based on key
  - Prevents duplicate submissions on network retry

---

## Authentication & Authorization

### Implemented
- [x] JWT-based authentication
- [x] Customer-scoped data isolation
- [x] Basic role field on User model

### Future Requirements
- [ ] **Role-Based Access Control (RBAC)**
  - Roles: Admin, Manager, Operator, Viewer
  - Permissions per role for CRUD operations

- [ ] **Multi-Factor Authentication (MFA)**
  - TOTP support
  - SMS/Email verification option

- [ ] **Session Management**
  - Token refresh mechanism
  - Session timeout configuration
  - Concurrent session limits

- [ ] **Password Policies**
  - Minimum complexity requirements
  - Password expiration
  - Password history

---

## Nesting & Optimization

### Implemented
- [x] GPU raster-based nesting with CuPy
- [x] Island-based GA for ratio optimization
- [x] Real-time preview during nesting (5-second intervals)
- [x] ILP solver for cutplan optimization
- [x] Multiple optimization strategies (Max Efficiency, Balanced, Min Markers)

### Future Requirements
- [ ] **Spyrrow CPU Refinement** - Post-GPU refinement for production markers
- [ ] **Marker Caching** - Cache computed markers for reuse across orders
- [ ] **Batch Nesting** - Queue multiple orders for overnight processing
- [ ] **Nesting Constraints UI** - User-configurable piece rotation, grouping rules
- [ ] **Multi-Color Joint Optimization** - Optimize markers across multiple colors simultaneously

---

## Cutplan & Cost Analysis

### Implemented
- [x] ILP-based marker selection
- [x] Cost breakdown (fabric, spreading, cutting, prep)
- [x] Multiple cutplan options generation

### Future Requirements
- [ ] **Configurable Cost Parameters** - Per-customer cost rates
- [ ] **Waste Tracking** - Track actual vs. estimated waste
- [ ] **Historical Cost Analysis** - Compare costs across orders
- [ ] **Export to ERP** - Integration with SAP, Oracle, etc.

---

## User Interface

### Implemented
- [x] 6-step workflow visualization
- [x] Order import from Excel/CSV
- [x] Pattern upload with auto-parsing
- [x] Fabric management
- [x] Real-time nesting progress with marker preview
- [x] Responsive design with dark mode

### Future Requirements
- [ ] **Drag-and-Drop File Upload** - Better UX for file uploads
- [ ] **Bulk Operations** - Select multiple orders for batch actions
- [ ] **Saved Views/Filters** - User-customizable order list views
- [ ] **Keyboard Shortcuts** - Power user productivity
- [ ] **Notification System** - In-app and email notifications for job completion
- [ ] **Dashboard Analytics** - Charts for utilization, costs, throughput

---

## Integration & Export

### Implemented
- [x] Excel/CSV order import
- [x] DXF/RUL pattern import (AAMA format)

### Future Requirements
- [ ] **Marker Export** - DXF, PLT, or PDF export for cutting machines
- [ ] **ERP Integration** - REST API for external systems
- [ ] **Cutting Machine Integration** - Direct send to Gerber, Lectra, etc.
- [ ] **Reporting Module** - PDF reports for cutplans, cost analysis

---

## Performance & Scalability

### Implemented
- [x] Background job processing (FastAPI BackgroundTasks)
- [x] PostgreSQL database with proper indexing

### Future Requirements
- [ ] **Celery Task Queue** - For long-running jobs
- [ ] **Redis Caching** - Cache frequently accessed data
- [ ] **Database Connection Pooling** - Handle high concurrency
- [ ] **Horizontal Scaling** - Multiple API instances behind load balancer
- [ ] **CDN for Static Assets** - Faster asset delivery

---

## DevOps & Monitoring

### Future Requirements
- [ ] **Docker Compose Production Setup** - Production-ready containers
- [ ] **CI/CD Pipeline** - Automated testing and deployment
- [ ] **Application Monitoring** - APM (Datadog, New Relic, etc.)
- [ ] **Log Aggregation** - Centralized logging (ELK, CloudWatch)
- [ ] **Health Checks** - Kubernetes-ready liveness/readiness probes
- [ ] **Backup Strategy** - Automated database backups

---

## Compliance & Security

### Future Requirements
- [ ] **Data Encryption at Rest** - Encrypt sensitive fields
- [ ] **API Rate Limiting** - Prevent abuse
- [ ] **Input Validation** - Comprehensive validation on all endpoints
- [ ] **CORS Configuration** - Proper cross-origin policies
- [ ] **Security Headers** - HSTS, CSP, X-Frame-Options
- [ ] **Penetration Testing** - Regular security audits

---

## Notes

- Priority items should be addressed before production deployment
- Each section can be expanded with specific user stories
- Update this document as requirements are implemented or added

---

*Last updated: 2026-02-10*
