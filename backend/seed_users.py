#!/usr/bin/env python3
"""
Seed script to create test users in the database.

Run with: python seed_users.py
"""

import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bcrypt
from backend.database import SessionLocal, engine, Base
from backend.models import User, Customer, CostConfig

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode('utf-8')


def seed_users():
    """Create test users in the database."""
    db = SessionLocal()

    try:
        # Check if users already exist
        existing_admin = db.query(User).filter(User.email == "admin@markermind.dev").first()
        if existing_admin:
            print("Test users already exist. Skipping seed.")
            print("\nExisting users:")
            users = db.query(User).all()
            for user in users:
                print(f"  - {user.email} ({user.role.value})")
            return

        # Create default customer for admin
        admin_customer = Customer(
            name="MarkerMind Admin",
            code="ADMIN",
        )
        db.add(admin_customer)
        db.flush()
        print(f"Created customer: {admin_customer.name} ({admin_customer.code})")

        # Create cost config for admin customer
        admin_cost_config = CostConfig(
            customer_id=admin_customer.id,
            name="Default",
            fabric_cost_per_yard=5.0,
            spreading_cost_per_yard=0.10,
            cutting_cost_per_inch=0.01,
            prep_cost_per_marker=2.0,
            max_ply_height=100,
        )
        db.add(admin_cost_config)

        # Create admin user
        admin_user = User(
            customer_id=admin_customer.id,
            email="admin@markermind.dev",
            password_hash=hash_password("admin123"),
            name="Admin User",
            role="admin",
            is_active="Y",
        )
        db.add(admin_user)
        print(f"Created user: {admin_user.email} (admin)")

        # Create test customer 1
        test_customer1 = Customer(
            name="Test Customer 1",
            code="TEST1",
        )
        db.add(test_customer1)
        db.flush()
        print(f"Created customer: {test_customer1.name} ({test_customer1.code})")

        # Create cost config for test customer 1
        test1_cost_config = CostConfig(
            customer_id=test_customer1.id,
            name="Default",
            fabric_cost_per_yard=5.0,
            spreading_cost_per_yard=0.10,
            cutting_cost_per_inch=0.01,
            prep_cost_per_marker=2.0,
            max_ply_height=100,
        )
        db.add(test1_cost_config)

        # Create test user 1
        test_user1 = User(
            customer_id=test_customer1.id,
            email="user1@test.com",
            password_hash=hash_password("password123"),
            name="Test User 1",
            role="operator",
            is_active="Y",
        )
        db.add(test_user1)
        print(f"Created user: {test_user1.email} (operator)")

        # Create test customer 2
        test_customer2 = Customer(
            name="Test Customer 2",
            code="TEST2",
        )
        db.add(test_customer2)
        db.flush()
        print(f"Created customer: {test_customer2.name} ({test_customer2.code})")

        # Create cost config for test customer 2
        test2_cost_config = CostConfig(
            customer_id=test_customer2.id,
            name="Default",
            fabric_cost_per_yard=5.0,
            spreading_cost_per_yard=0.10,
            cutting_cost_per_inch=0.01,
            prep_cost_per_marker=2.0,
            max_ply_height=100,
        )
        db.add(test2_cost_config)

        # Create test user 2
        test_user2 = User(
            customer_id=test_customer2.id,
            email="user2@test.com",
            password_hash=hash_password("password123"),
            name="Test User 2",
            role="operator",
            is_active="Y",
        )
        db.add(test_user2)
        print(f"Created user: {test_user2.email} (operator)")

        db.commit()
        print("\n✓ All test users created successfully!")
        print("\nLogin credentials:")
        print("  admin@markermind.dev / admin123")
        print("  user1@test.com / password123")
        print("  user2@test.com / password123")

    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print("Seeding test users...")
    seed_users()
