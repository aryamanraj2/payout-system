# Design notes

The README covers setup, the API, edge cases and trade-offs. These are the
lower-level details.

## Ledger

There is no balance column anywhere. Each money movement is an append-only row
in `ledger_entries` with a signed amount, and the balance is the sum. This
means the audit trail can't disagree with the balance — it *is* the balance.

Every logical event has a deterministic idempotency key with a unique index:

| Event | Key |
|---|---|
| advance for a sale | `advance_payouts.sale_id` (unique) |
| credit for approved sale | `final:{sale_id}` |
| adjustment for rejected sale | `reject:{sale_id}` |
| withdrawal debit | `withdraw:{payout_id}` |
| failure reversal | `reversal:{payout_id}` |

A retried request or duplicated webhook generates the same key, hits the
unique index, and gets treated as already-done. The SELECT filters in the job
are just optimisations — the constraints are what actually guarantee
correctness under concurrency.

## Concurrency

SQLite specifics (all in `app/db.py`):

- `PRAGMA foreign_keys=ON` per connection — FKs are off by default.
- Every transaction opens with `BEGIN IMMEDIATE`, taking the write lock up
  front. Without this, two withdrawals could both read balance 68 and both
  spend it. It has to be emitted from the engine's `begin` event; issuing it
  through the session fails because SQLAlchemy has already opened a
  transaction by then.
- `busy_timeout=5000` so a queued writer waits instead of erroring.

On Postgres the same paths use `SELECT ... FOR UPDATE` on the user row
(`_lock_user` in the withdrawal service); the SQLite branch skips it because
the database-wide write lock already covers it.

The withdrawal is the hot path: balance check and debit happen in one
serialised transaction. `test_concurrent_withdrawals_one_wins` runs two real
threads at the same balance; with the locking removed both succeed (verified
while writing the test), with it exactly one wins.

Withdrawals debit at initiation, not completion. Debiting at completion would
leave a window where an in-flight payout's money still looks available.

## Advance job

Per sale, inside a savepoint:

1. insert the `advance_payouts` row
2. flush — this is where a duplicate (another job instance won the race)
   raises IntegrityError, before any money moves
3. call the gateway

The order matters: transfer-then-insert would pay the user and then roll back
the record of having paid. A failed transfer rolls back only that sale's
savepoint, so one bad sale doesn't kill the batch and the sale stays eligible
for the next run.

## Q2 recovery

When a payout hits failed/cancelled/rejected, a `WITHDRAWAL_REVERSAL` entry
credits the amount back. The original debit is never touched — the ledger
keeps both rows (`-68`, `+68`) forever.

Two independent things prevent a double credit:

1. the state machine — terminal states have no exits, so the reversal code
   can't run twice through normal flow;
2. the `reversal:{payout_id}` unique key — even if the status were somehow
   rewound (bug, manual DB edit), the second insert collides and is ignored.

The reversal insert sits in a savepoint so a duplicate key only rolls back
the reversal, not the status update that came with it.

Failed/cancelled/rejected payouts are excluded from the 24-hour withdrawal
limit. If they counted, a user whose payout failed couldn't retry for a day,
which contradicts Q2.

## Money and datetime types

Two custom SQLAlchemy types in `models.py`:

- `Money` — stores Decimal as TEXT. SQLAlchemy's `Numeric` round-trips
  through float on SQLite, which defeats the point of using Decimal. Values
  are quantised to 2 places (ROUND_HALF_UP) on write. For Postgres, swap the
  impl to NUMERIC(12,2).
- `UTCDateTime` — SQLite drops timezone info, so aware datetimes come back
  naive and datetime arithmetic (e.g. computing `retry_after`) raises
  TypeError. This type normalises to aware UTC both ways.

## Things that bit me

- SQLAlchemy orders INSERTs by mapper relationships. `ledger_entries.payout_id`
  is a plain FK column with no relationship(), so the ledger row was inserted
  before the payout it references and the FK constraint failed. Fixed with a
  flush between the two adds.
- A threaded test of the advance job always ended with one worker doing all
  the sales — the locking is so effective the race never happens naturally.
  The duplicate-advance path is instead tested by feeding the job a stale
  sale list directly.
- f-strings on `str`-based enums print `PayoutStatus.COMPLETED` (not
  `completed`) on Python 3.11+, which leaked into 409 error messages. Client
  messages format with `.value`.
