"""Legal payout transitions. Terminal states have no outgoing edges, so e.g.
completed -> failed is impossible by construction."""

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

# these credit the money back to the user's balance (Q2)
TERMINAL_FAILURE: set[PayoutStatus] = {
    PayoutStatus.FAILED,
    PayoutStatus.CANCELLED,
    PayoutStatus.REJECTED,
}


def can_transition(current: PayoutStatus, new: PayoutStatus) -> bool:
    return new in PAYOUT_TRANSITIONS[current]
