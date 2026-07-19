"""Domain exceptions. Services raise these; main.py maps them to HTTP once.

400 invalid input / 404 not found / 409 illegal transition
422 insufficient balance / 429 withdrawal rate limit
"""

from datetime import datetime
from decimal import Decimal


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_payload(self) -> dict:
        return {"error": self.code, "detail": self.message}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class InvalidTransition(DomainError):
    status_code = 409
    code = "invalid_transition"


class InsufficientBalance(DomainError):
    status_code = 422
    code = "insufficient_balance"

    def __init__(self, requested: Decimal, available: Decimal):
        super().__init__(
            f"requested {requested} but withdrawable balance is {available}"
        )
        self.requested = requested
        self.available = available

    def to_payload(self) -> dict:
        return {
            **super().to_payload(),
            "requested": str(self.requested),
            "available": str(self.available),
        }


class WithdrawalRateLimited(DomainError):
    status_code = 429
    code = "withdrawal_rate_limited"

    def __init__(self, retry_after: datetime):
        super().__init__(
            f"a withdrawal was already made in the last 24 hours; "
            f"next allowed at {retry_after.isoformat()}"
        )
        self.retry_after = retry_after

    def to_payload(self) -> dict:
        return {**super().to_payload(), "retry_after": self.retry_after.isoformat()}
