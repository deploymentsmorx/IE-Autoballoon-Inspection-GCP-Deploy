from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core import database as dbmod
from app.core.security import hash_password
from app.services import customer_service, license_json, local_store


class LicenseJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "smorx.json"
        self.original_path = local_store.DB_PATH
        self.original_pg = dbmod.USE_POSTGRES
        self.original_session = dbmod.SessionLocal
        local_store.DB_PATH = self.db_path
        dbmod.USE_POSTGRES = False
        dbmod.SessionLocal = None
        local_store.ensure_seed(hash_password)
        org = {"id": "org-1", "name": "Smorx", "tenant_id": "ORG-1", "status": "Active"}
        db = local_store.load()
        db["organizations"].append(org)
        local_store.save(db)

    def tearDown(self):
        local_store.DB_PATH = self.original_path
        dbmod.USE_POSTGRES = self.original_pg
        dbmod.SessionLocal = self.original_session
        self.tmp.cleanup()

    def test_create_customer_and_device_roundtrip(self):
        customer = customer_service.create_customer(
            organization="Smorx",
            license_key="AB-TEST-1",
            org_id="org-1",
            install_password="install123",
            require_device_approval=True,
            product_code="autoballoon",
        )
        self.assertEqual(customer["license_key"], "AB-TEST-1")
        rows = customer_service.list_customers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["organization"], "Smorx")

        device = customer_service.add_approved_device(
            customer["customer_id"],
            "DEV-013CF82D-B058-40D5-8321",
            "DESKTOP-8DDCU07",
        )
        self.assertEqual(device["status"], "Pending")
        listed = customer_service.list_approved_devices(customer["customer_id"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["hostname"], "DESKTOP-8DDCU07")

        saved = json.loads(self.db_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["customers"]), 1)
        self.assertEqual(len(saved["approved_devices"]), 1)

    def test_license_json_rejects_duplicate_device(self):
        customer = license_json.create_customer(
            organization="Smorx",
            license_key="AB-TEST-2",
            org_id="org-1",
            install_password="install123",
        )
        license_json.add_approved_device(
            customer["customer_id"], "DEV-AAAA", "HOST-A"
        )
        with self.assertRaises(Exception):
            license_json.add_approved_device(
                customer["customer_id"], "DEV-AAAA", "HOST-A"
            )


if __name__ == "__main__":
    unittest.main()
