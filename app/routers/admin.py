from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ReconcileRequest, SaleOut
from app.services.reconciliation_service import reconcile_sale

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/sales/{sale_id}/reconcile", response_model=SaleOut)
def reconcile(sale_id: str, payload: ReconcileRequest, db: Session = Depends(get_db)):
    """Approve or reject a pending sale and net it against any advance paid.

    409 if the sale has already been reconciled -- this happens exactly once.
    """
    return reconcile_sale(db, sale_id, payload.status)
