"""FastAPI application.

Swagger UI at /docs is the demo surface: every flow in the assignment can be
walked end to end from there.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import init_db
from app.errors import DomainError, WithdrawalRateLimited
from app.routers import admin, jobs, sales, users


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

@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError):
    """One place where business-rule violations become HTTP responses, so
    services never have to import FastAPI."""
    headers = {}
    if isinstance(exc, WithdrawalRateLimited):
        # Standard 429 semantics, in seconds, alongside the ISO timestamp.
        delta = exc.retry_after - exc.retry_after.now(exc.retry_after.tzinfo)
        headers["Retry-After"] = str(max(0, int(delta.total_seconds())))
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_payload(), headers=headers
    )


app.include_router(sales.router)
app.include_router(jobs.router)
app.include_router(admin.router)
app.include_router(users.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
