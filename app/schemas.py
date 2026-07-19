"""Pydantic request/response DTOs.

Money crosses the API boundary as Decimal, which Pydantic serialises to a JSON
number without a float round-trip.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import LedgerEntryType, SaleStatus


class SaleCreate(BaseModel):
    user_id: str = Field(..., examples=["john_doe"])
    brand: str = Field(..., examples=["brand_1"])
    earning: Decimal = Field(..., ge=0, examples=[Decimal("40.00")])


class SaleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    brand: str
    earning: Decimal
    status: SaleStatus
    reconciled_at: datetime | None
    created_at: datetime


class AdvanceJobResult(BaseModel):
    advances_paid: int
    skipped: int
    failed: int


class ReconcileRequest(BaseModel):
    status: SaleStatus = Field(..., examples=[SaleStatus.APPROVED])


class BalanceOut(BaseModel):
    user_id: str
    withdrawable_balance: Decimal


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entry_type: LedgerEntryType
    amount: Decimal
    sale_id: str | None
    payout_id: str | None
    idempotency_key: str
    created_at: datetime
