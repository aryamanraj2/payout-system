from enum import Enum


class SaleStatus(str, Enum):
    """Lifecycle of an affiliate sale. A sale is reconciled exactly once:
    pending -> approved, or pending -> rejected."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PayoutStatus(str, Enum):
    """Lifecycle of an outbound withdrawal, driven by the payment gateway."""

    INITIATED = "initiated"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class LedgerEntryType(str, Enum):
    """Why a ledger row exists. Advances are deliberately absent: an advance is
    money that already left the system, so it never enters the withdrawable
    balance. Only reconciliation and withdrawals move the balance."""

    FINAL_CREDIT = "FINAL_CREDIT"                  # approved sale: earning - advance
    REJECTION_ADJUSTMENT = "REJECTION_ADJUSTMENT"  # rejected sale: -advance (clawback)
    WITHDRAWAL_DEBIT = "WITHDRAWAL_DEBIT"          # withdrawal initiated: -amount
    WITHDRAWAL_REVERSAL = "WITHDRAWAL_REVERSAL"    # withdrawal failed: +amount
