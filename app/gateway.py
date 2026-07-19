"""Payment gateway stub.

In production this is an HTTP call, which can't be atomic with our DB
transaction — a crash between transfer and commit could re-send on the next
run. The proper fix is an outbox (persist the intent in the same transaction,
deliver from a worker). Out of scope here; the stub always succeeds and tests
monkeypatch it to simulate failures.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class TransferFailed(Exception):
    pass


def transfer_funds_external(user_id: str, amount: Decimal, reference: str) -> str:
    logger.info("gateway: transferring %s to %s (ref=%s)", amount, user_id, reference)
    return f"gw_{reference}"
