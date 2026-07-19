from enum import Enum


class SaleStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PayoutStatus(str, Enum):
    INITIATED = "initiated"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class LedgerEntryType(str, Enum):
    """Advances are deliberately not a ledger type — that money already left
    the system and is never part of the withdrawable balance."""

    FINAL_CREDIT = "FINAL_CREDIT"                  # approved sale: earning - advance
    REJECTION_ADJUSTMENT = "REJECTION_ADJUSTMENT"  # rejected sale: -advance
    WITHDRAWAL_DEBIT = "WITHDRAWAL_DEBIT"          # withdrawal: -amount
    WITHDRAWAL_REVERSAL = "WITHDRAWAL_REVERSAL"    # failed withdrawal: +amount
