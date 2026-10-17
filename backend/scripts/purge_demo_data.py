"""Wipe commercial test data from Postgres. Keeps super_admin only.

Run from backend folder:
  python scripts/purge_demo_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select, update

from app.core import database as dbmod
from app.models import ApprovedDevice, Customer, Installation, Job, Organization, User


def purge() -> None:
    if not dbmod._try_connect() or dbmod.SessionLocal is None:
        print("Postgres not connected — check backend/.env DATABASE_URL")
        sys.exit(1)

    with dbmod.SessionLocal() as db:
        j = db.execute(delete(Job)).rowcount
        inst = db.execute(delete(Installation)).rowcount
        dev = db.execute(delete(ApprovedDevice)).rowcount
        eng = db.execute(
            delete(User).where(User.role != "super_admin")
        ).rowcount
        db.execute(
            update(User)
            .where(User.role == "super_admin")
            .values(customer_id=None, org_id=None, must_change_password=False)
        )
        cust = db.execute(delete(Customer)).rowcount
        org = db.execute(delete(Organization)).rowcount
        db.commit()

        admin = db.scalar(select(User).where(User.role == "super_admin"))
        print("Purge complete (Postgres):")
        print(f"  jobs removed:           {j}")
        print(f"  installations removed:  {inst}")
        print(f"  approved devices removed: {dev}")
        print(f"  engineers removed:      {eng}")
        print(f"  customers removed:      {cust}")
        print(f"  organizations removed:  {org}")
        if admin:
            print(f"  kept super admin:       {admin.email}")
        else:
            print("  WARNING: no super_admin user found")


if __name__ == "__main__":
    purge()
