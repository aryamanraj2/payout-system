"""FastAPI application.

Swagger UI at /docs is the demo surface: every flow in the assignment can be
walked end to end from there.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.routers import jobs, sales


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="User Payout Management System",
    description=(
        "Affiliate payout system with advance payouts, reconciliation, "
        "withdrawals, and failed-payout recovery. Balances are derived from an "
        "append-only ledger; idempotency is enforced by database constraints."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(sales.router)
app.include_router(jobs.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
