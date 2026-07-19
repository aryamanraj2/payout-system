"""Pydantic request/response DTOs.

Money crosses the API boundary as Decimal, which Pydantic serialises to a JSON
number without a float round-trip.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import SaleStatus


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
