"""Legal payout transitions.

Terminal states have no outgoing edges, which makes `completed -> failed`
structurally impossible rather than a check someone can forget to write.
"""

from app.enums import PayoutStatus

PAYOUT_TRANSITIONS: dict[PayoutStatus, set[PayoutStatus]] = {
    PayoutStatus.INITIATED: {
        PayoutStatus.PROCESSING,
        PayoutStatus.CANCELLED,
        PayoutStatus.FAILED,
    },
    PayoutStatus.PROCESSING: {
        PayoutStatus.COMPLETED,
        PayoutStatus.FAILED,
        PayoutStatus.REJECTED,
    },
    PayoutStatus.COMPLETED: set(),
    PayoutStatus.FAILED: set(),
    PayoutStatus.CANCELLED: set(),
    PayoutStatus.REJECTED: set(),
}

# Q2: these three outcomes return the money to the withdrawable balance.
TERMINAL_FAILURE: set[PayoutStatus] = {
    PayoutStatus.FAILED,
    PayoutStatus.CANCELLED,
    PayoutStatus.REJECTED,
}


def can_transition(current: PayoutStatus, new: PayoutStatus) -> bool:
    return new in PAYOUT_TRANSITIONS[current]
