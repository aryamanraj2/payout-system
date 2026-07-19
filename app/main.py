from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import init_db
from app.errors import DomainError, WithdrawalRateLimited
from app.routers import admin, jobs, sales, users, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="User Payout Management System",
    description=(
        "Affiliate payout system: advance payouts, reconciliation, "
        "withdrawals and failed-payout recovery."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError):
    headers = {}
    if isinstance(exc, WithdrawalRateLimited):
        # standard Retry-After header in seconds, alongside the ISO timestamp
        delta = exc.retry_after - exc.retry_after.now(exc.retry_after.tzinfo)
        headers["Retry-After"] = str(max(0, int(delta.total_seconds())))
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_payload(), headers=headers
    )


app.include_router(sales.router)
app.include_router(jobs.router)
app.include_router(admin.router)
app.include_router(users.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
