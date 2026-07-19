from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import SaleStatus
from app.models import Sale, User, new_id
from app.schemas import SaleCreate, SaleOut

router = APIRouter(prefix="/sales", tags=["sales"])


@router.post("", response_model=SaleOut, status_code=201)
def create_sale(payload: SaleCreate, db: Session = Depends(get_db)):
    """Create a sale. Every sale enters the system as `pending`."""
    # Users are created on first sight; the assignment has no user-management
    # flow and a missing user would otherwise be an FK error.
    if db.get(User, payload.user_id) is None:
        db.add(User(id=payload.user_id))

    sale = Sale(
        id=new_id(),
        user_id=payload.user_id,
        brand=payload.brand,
        earning=payload.earning,
        status=SaleStatus.PENDING,
    )
    db.add(sale)
    db.commit()
    return sale


@router.get("", response_model=list[SaleOut])
def list_sales(
    user_id: str | None = Query(None),
    status: SaleStatus | None = Query(None),
    db: Session = Depends(get_db),
):
    stmt = select(Sale)
    if user_id:
        stmt = stmt.where(Sale.user_id == user_id)
    if status:
        stmt = stmt.where(Sale.status == status)
    return list(db.execute(stmt.order_by(Sale.created_at)).scalars().all())
