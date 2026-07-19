"""Payment gateway stub.

Isolated behind one function so the integration seam is obvious. In production
this is an HTTP call to a payments provider, and that changes the failure model
in a way worth stating plainly:

A network call cannot be made atomic with a database transaction. If the
transfer succeeds but the surrounding transaction later fails to commit, money
has left the building with no record of it -- and the next job run would send
it again. The fix is an outbox: persist the intent in the same transaction as
the advance row, commit, then have a separate worker perform the transfer and
mark it sent, retrying on a durable `pending -> sent` state carried on the
transfer itself.

At assignment scale the in-transaction call is fine and the risk window is
microseconds, but the production path is the outbox. See README.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class TransferFailed(Exception):
    """Raised when the gateway declines or errors on a transfer."""


def transfer_funds_external(user_id: str, amount: Decimal, reference: str) -> str:
    """Send `amount` to `user_id`. Returns a gateway transaction reference.

    The stub always succeeds. Tests monkeypatch it to simulate failures.
    """
    logger.info("gateway: transferring %s to %s (ref=%s)", amount, user_id, reference)
    return f"gw_{reference}"
