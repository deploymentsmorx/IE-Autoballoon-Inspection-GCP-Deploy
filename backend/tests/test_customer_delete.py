from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import database as dbmod
from app.core.database import Base
from app.models import ApprovedDevice, Customer, Installation, Job, Organization, User
from app.services import auth_service, customer_service


class DeleteCustomerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.original_session_local = dbmod.SessionLocal
        self.original_use_postgres = dbmod.USE_POSTGRES
        dbmod.SessionLocal = self.sessions
        dbmod.USE_POSTGRES = True

    def tearDown(self):
        dbmod.SessionLocal = self.original_session_local
        dbmod.USE_POSTGRES = self.original_use_postgres
        self.engine.dispose()

    def add_org_and_customer(self, customer_id="CUST-1", org_id="ORG-1"):
        with self.sessions() as db:
            db.add(Organization(id=org_id, name="Example Co", tenant_id=org_id))
            db.flush()
            db.add(
                Customer(
                    customer_id=customer_id,
                    organization="Example Co",
                    license_key="CS-TEST-" + customer_id,
                    org_id=org_id,
                    product_code="costsheet",
                )
            )
            db.flush()
            db.add(
                User(
                    id="USER-" + customer_id,
                    username="engineer-" + customer_id,
                    email="engineer-" + customer_id + "@example.com",
                    full_name="Example Engineer",
                    password_hash="hashed",
                    role="engineer",
                    org_id=org_id,
                    customer_id=customer_id,
                )
            )
            db.commit()

    def test_delete_customer_cleans_linked_records_and_preserves_shared_org_and_history(self):
        self.add_org_and_customer()
        with self.sessions() as db:
            db.add(
                User(
                    id="USER-CUST-1-SECOND",
                    username="second-engineer-cust-1",
                    email="second-engineer-cust-1@example.com",
                    full_name="Second Example Engineer",
                    password_hash="hashed",
                    role="engineer",
                    org_id="ORG-1",
                    customer_id="CUST-1",
                )
            )
            db.add(
                Customer(
                    customer_id="CUST-2",
                    organization="Example Co",
                    license_key="CS-TEST-CUST-2",
                    org_id="ORG-1",
                    product_code="costsheet",
                )
            )
            db.add(
                User(
                    id="USER-CUST-2",
                    username="engineer-cust-2",
                    email="engineer-cust-2@example.com",
                    full_name="Other Engineer",
                    password_hash="hashed",
                    role="engineer",
                    org_id="ORG-1",
                    customer_id="CUST-2",
                )
            )
            db.add(
                Installation(
                    installation_id="INSTALL-1",
                    customer_id="CUST-1",
                    device_id="DEV-1",
                    hostname="PC-1",
                    status="Active",
                )
            )
            db.add(
                ApprovedDevice(
                    id="DEVICE-1",
                    customer_id="CUST-1",
                    device_id="DEV-1",
                    hostname="PC-1",
                    status="Active",
                )
            )
            db.add(
                Job(
                    job_id="JOB-1",
                    customer_id="CUST-1",
                    user_id="USER-CUST-1",
                    installation_id="INSTALL-1",
                )
            )
            db.commit()

        result = auth_service.delete_engineer("USER-CUST-1")

        self.assertTrue(result["deleted_customer"])
        self.assertEqual(result["deleted_users"], 2)
        self.assertEqual(result["deleted_devices"], 1)
        self.assertEqual(result["deleted_installations"], 1)
        self.assertEqual(result["deleted_organizations"], 0)

        with self.sessions() as db:
            self.assertIsNone(db.get(Customer, "CUST-1"))
            self.assertIsNotNone(db.get(Customer, "CUST-2"))
            self.assertIsNone(db.get(User, "USER-CUST-1"))
            self.assertIsNone(db.get(User, "USER-CUST-1-SECOND"))
            self.assertIsNotNone(db.get(User, "USER-CUST-2"))
            self.assertIsNone(db.get(ApprovedDevice, "DEVICE-1"))
            self.assertIsNone(db.get(Installation, "INSTALL-1"))
            self.assertIsNotNone(db.get(Organization, "ORG-1"))
            job = db.get(Job, "JOB-1")
            self.assertIsNotNone(job)
            self.assertIsNone(job.customer_id)
            self.assertIsNone(job.user_id)
            self.assertIsNone(job.installation_id)

    def test_delete_customer_removes_orphaned_organization(self):
        self.add_org_and_customer()

        result = customer_service.delete_customer("CUST-1")

        self.assertEqual(result["deleted_organizations"], 1)
        with self.sessions() as db:
            self.assertIsNone(db.get(Organization, "ORG-1"))


if __name__ == "__main__":
    unittest.main()

