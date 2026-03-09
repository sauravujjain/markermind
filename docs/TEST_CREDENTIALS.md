# Test User Credentials

## Development Environment Logins

Use these credentials to log into the MarkerMind application during development and testing.

---

### Admin User

| Field | Value |
|-------|-------|
| Email | `admin@markermind.dev` |
| Password | `admin123` |
| Role | `admin` |
| Customer | Default Customer |

---

### Test User 1

| Field | Value |
|-------|-------|
| Email | `user1@test.com` |
| Password | `password123` |
| Role | `user` |
| Customer | Test Customer 1 |

---

### Test User 2

| Field | Value |
|-------|-------|
| Email | `user2@test.com` |
| Password | `password123` |
| Role | `user` |
| Customer | Test Customer 2 |

---

## Creating Test Users

To create these users in the database, run the following SQL or use the registration endpoint:

```bash
# Via API (registration endpoint)
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@markermind.dev",
    "password": "admin123",
    "name": "Admin User"
  }'
```

Or add to your seed script:

```python
from backend.models import User, Customer
from backend.database import SessionLocal
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

db = SessionLocal()

# Create customer
customer = Customer(name="Default Customer", code="DEFAULT")
db.add(customer)
db.flush()

# Create admin user
admin = User(
    email="admin@markermind.dev",
    hashed_password=pwd_context.hash("admin123"),
    name="Admin User",
    role="admin",
    customer_id=customer.id,
)
db.add(admin)

# Create test users
test_customer = Customer(name="Test Customer 1", code="TEST1")
db.add(test_customer)
db.flush()

user1 = User(
    email="user1@test.com",
    hashed_password=pwd_context.hash("password123"),
    name="Test User 1",
    role="user",
    customer_id=test_customer.id,
)
db.add(user1)

db.commit()
```

---

## Quick Reference

| User | Email | Password |
|------|-------|----------|
| Admin | admin@markermind.dev | admin123 |
| User 1 | user1@test.com | password123 |
| User 2 | user2@test.com | password123 |

---

**Note:** These credentials are for development only. Do not use in production.
