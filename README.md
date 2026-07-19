# User Payout Management System

Affiliate sales payout system — SDE intern assignment. Pending sales get a 10%
advance payout, an admin reconciles them to approved/rejected, users withdraw
their balance, and failed payouts are credited back (Q2).

Stack: Python 3.11+, FastAPI, SQLAlchemy 2.0, SQLite, pytest.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --reload
```

Swagger UI: http://127.0.0.1:8000/docs

Tests:

```bash
python -m pytest -v
```

## The assignment example

`seed.py` creates `john_doe` with 3 pending sales of ₹40. Expected final
payout: ₹68.

```bash
curl -X POST 127.0.0.1:8000/jobs/advance-payouts/run
# {"advances_paid":3,"skipped":0,"failed":0}

curl -X POST 127.0.0.1:8000/admin/sales/sale_1/reconcile -H 'Content-Type: application/json' -d '{"status":"rejected"}'
curl -X POST 127.0.0.1:8000/admin/sales/sale_2/reconcile -H 'Content-Type: application/json' -d '{"status":"approved"}'
curl -X POST 127.0.0.1:8000/admin/sales/sale_3/reconcile -H 'Content-Type: application/json' -d '{"status":"approved"}'

curl 127.0.0.1:8000/users/john_doe/balance
# {"user_id":"john_doe","withdrawable_balance":"68.00"}    (-4 + 36 + 36)
```

Q2 — failed payout recovery:

```bash
curl -X POST 127.0.0.1:8000/users/john_doe/withdrawals -H 'Content-Type: application/json' -d '{"amount":68.00}'
# note the payout id, then:
curl -X POST 127.0.0.1:8000/webhooks/payouts/<payout_id> -H 'Content-Type: application/json' -d '{"status":"failed"}'
curl 127.0.0.1:8000/users/john_doe/balance     # back to 68.00, can withdraw again
```

(Use `127.0.0.1`, not `localhost` — curl may try IPv6 first and get an empty
reply.)

## How it works

Balances are never stored. Every money movement is a row in an append-only
`ledger_entries` table and the withdrawable balance is just the sum:

```
approved sale:   FINAL_CREDIT          +(earning - advance)
rejected sale:   REJECTION_ADJUSTMENT  -advance
withdrawal:      WITHDRAWAL_DEBIT      -amount
failed payout:   WITHDRAWAL_REVERSAL   +amount
```

Advances are *not* in the ledger — that money already left the system, so it
is never part of the withdrawable balance. Reconciliation nets it out.

Idempotency is enforced by unique constraints, not application checks:

- `advance_payouts.sale_id UNIQUE` — a sale can never get a second advance,
  even if the job runs twice or two instances race.
- `ledger_entries.idempotency_key UNIQUE` — keys like `final:{sale_id}` and
  `reversal:{payout_id}` make duplicate webhooks and re-runs no-ops.

## Tables

- `users` — id, created_at
- `sales` — user_id, brand, earning, status (pending/approved/rejected)
- `advance_payouts` — sale_id (unique), user_id, amount
- `payouts` — user_id, amount, status (initiated/processing/completed/failed/cancelled/rejected)
- `ledger_entries` — user_id, entry_type, signed amount, idempotency_key (unique)

Payout status transitions are restricted by a state machine
(`app/state_machine.py`). Terminal states have no exits, so a late "failed"
webhook can't reverse a completed payout.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/sales` | create sale (starts as pending) |
| GET | `/sales?user_id=&status=` | list/filter |
| POST | `/jobs/advance-payouts/run` | advance job, idempotent |
| POST | `/admin/sales/{id}/reconcile` | body `{"status":"approved"|"rejected"}` |
| GET | `/users/{id}/balance` | |
| GET | `/users/{id}/ledger` | audit trail |
| GET | `/users/{id}/payouts` | |
| POST | `/users/{id}/withdrawals` | body `{"amount": 68.00}` |
| POST | `/webhooks/payouts/{id}` | gateway status callback |

Errors: 400 bad input, 404 not found, 409 illegal transition, 422 insufficient
balance, 429 withdrawal rate limit (with `retry_after`).

## Edge cases handled

| Scenario | Handling |
|---|---|
| Advance job re-run / two instances racing | unique constraint on sale_id, one advance per sale |
| Gateway transfer fails mid-job | savepoint per sale, batch continues, sale retried next run |
| Two concurrent withdrawals | BEGIN IMMEDIATE serialises check + debit (tested with real threads) |
| Duplicate webhook | same-status no-op, returns 200 so the gateway stops retrying |
| Reconcile a sale twice | 409, sales reconcile exactly once |
| Webhook completed → failed | state machine rejects it |
| All sales rejected after advances | balance goes negative, nets against future earnings |
| Failed payout retry | reversal restores balance; failed payouts don't count toward the 24h limit |
| Rejected sale with no advance | nothing owed, no ledger row written |
| Float rounding | Decimal end-to-end, money stored as text, ROUND_HALF_UP to 2 places |

## Design decisions

- **Derived balance vs cached column** — chose SUM(ledger). Can't drift from
  history. O(n) per read doesn't matter at this scale; production would cache
  a balance column updated in the same transaction.
- **Debit at initiation, not completion** — makes the balance check atomic
  with the spend so a race can't double-withdraw. Cost: money is "in flight"
  during processing; failures come back via a reversal entry.
- **Failed payouts don't hold the 24h slot** — otherwise Q2's "allow another
  withdrawal" would be impossible for a day. This is an interpretation of the
  spec, and it's covered by tests.
- **Negative balances persist** — a user who owes money (advances on rejected
  sales) nets it against future earnings; withdrawals are blocked meanwhile.
- **SQLite** — zero-setup for review. The locking code has a Postgres path
  (`SELECT ... FOR UPDATE`) in the one function that needs it.

Not done (out of scope): webhook signature verification, auth on `/admin`,
outbox pattern for the gateway call (a crash between transfer and commit could
re-send — noted in `app/gateway.py`).

## Layout

```
app/
  main.py            FastAPI app + error handlers
  db.py              engine, sessions, SQLite pragmas
  models.py          ORM models
  enums.py           statuses and ledger entry types
  state_machine.py   payout transitions
  errors.py          domain exceptions -> HTTP codes
  gateway.py         payment gateway stub
  services/          business logic (advance, reconciliation, balance,
                     withdrawal, recovery)
  routers/           sales, jobs, admin, users, webhooks
tests/               65 tests
seed.py              assignment example data
docs/DESIGN.md       design notes
```
