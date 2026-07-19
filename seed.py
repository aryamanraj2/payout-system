"""Seed the database with the assignment's worked example.

    3 pending sales for john_doe, Rs 40 each (Rs 120 total pending).

After seeding, the whole PDF scenario can be reproduced from Swagger
(http://127.0.0.1:8000/docs) or curl:

    1. POST /jobs/advance-payouts/run         -> 3 advances of Rs 4 (Rs 12)
    2. POST /admin/sales/{id}/reconcile       -> reject one, approve two
    3. GET  /users/john_doe/balance           -> 68.00

Usage:
    python seed.py            # refuses to touch an existing payouts.db
    python seed.py --reset    # wipe and reseed
"""

import sys
from decimal import Decimal
from pathlib import Path

from app.db import SessionLocal, init_db
from app.models import Sale, User

DB_FILE = Path("payouts.db")


def main() -> None:
    if DB_FILE.exists():
        if "--reset" not in sys.argv:
            sys.exit(
                f"{DB_FILE} already exists. Re-run with --reset to wipe and reseed."
            )
        DB_FILE.unlink()

    init_db()
    db = SessionLocal()
    try:
        db.add(User(id="john_doe"))
        for i in range(3):
            db.add(
                Sale(
                    id=f"sale_{i + 1}",
                    user_id="john_doe",
                    brand="brand_1",
                    earning=Decimal("40.00"),
                )
            )
        db.commit()
    finally:
        db.close()

    print("Seeded the assignment's worked example:")
    print("  user john_doe with 3 pending sales of Rs 40 (sale_1, sale_2, sale_3)")
    print()
    print("Next:")
    print("  uvicorn app.main:app --reload")
    print("  open http://127.0.0.1:8000/docs")
    print()
    print("Reproduce the PDF (expected final balance: 68.00):")
    print("  curl -X POST 127.0.0.1:8000/jobs/advance-payouts/run")
    print("  curl -X POST 127.0.0.1:8000/admin/sales/sale_1/reconcile "
          "-H 'Content-Type: application/json' -d '{\"status\":\"rejected\"}'")
    print("  curl -X POST 127.0.0.1:8000/admin/sales/sale_2/reconcile "
          "-H 'Content-Type: application/json' -d '{\"status\":\"approved\"}'")
    print("  curl -X POST 127.0.0.1:8000/admin/sales/sale_3/reconcile "
          "-H 'Content-Type: application/json' -d '{\"status\":\"approved\"}'")
    print("  curl 127.0.0.1:8000/users/john_doe/balance")


if __name__ == "__main__":
    main()
