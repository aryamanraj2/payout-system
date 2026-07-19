"""Stage 5.3: the webhook endpoint, exercised through the real HTTP stack.

Everything here goes through TestClient -- routers, Pydantic serialisation,
and the DomainError -> HTTP exception handler -- because none of that is
touched by the service-level tests.
"""

import pytest


@pytest.fixture
def payout_id(client):
    """A live initiated payout of Rs 68, built entirely over HTTP."""
    sale_ids = [
        client.post(
            "/sales",
            json={"user_id": "john_doe", "brand": "brand_1", "earning": 40},
        ).json()["id"]
        for _ in range(3)
    ]
    assert client.post("/jobs/advance-payouts/run").json()["advances_paid"] == 3

    for sale_id, status in zip(sale_ids, ["rejected", "approved", "approved"]):
        r = client.post(f"/admin/sales/{sale_id}/reconcile", json={"status": status})
        assert r.status_code == 200

    r = client.post("/users/john_doe/withdrawals", json={"amount": 68.00})
    assert r.status_code == 201
    return r.json()["id"]


def _balance(client) -> str:
    return client.get("/users/john_doe/balance").json()["withdrawable_balance"]


def test_webhook_advances_payout_status(client, payout_id):
    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "processing"})
    assert r.status_code == 200
    assert r.json()["status"] == "processing"


def test_webhook_failure_credits_balance(client, payout_id):
    """Q2 over HTTP: the gateway reports failure, the balance comes back."""
    assert _balance(client) == "0.00"

    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})

    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert _balance(client) == "68.00"

    # The audit trail shows both movements.
    types = [e["entry_type"] for e in client.get("/users/john_doe/ledger").json()]
    assert types.count("WITHDRAWAL_DEBIT") == 1
    assert types.count("WITHDRAWAL_REVERSAL") == 1


def test_webhook_redelivery_returns_200_and_credits_once(client, payout_id):
    """Gateways retry until they see 2xx. A redelivery must therefore get 200,
    not 409 -- an error would make the gateway retry forever."""
    first = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})
    second = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert _balance(client) == "68.00"


def test_webhook_illegal_transition_is_409(client, payout_id):
    client.post(f"/webhooks/payouts/{payout_id}", json={"status": "processing"})
    client.post(f"/webhooks/payouts/{payout_id}", json={"status": "completed"})

    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})

    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "invalid_transition"
    assert "completed -> failed" in body["detail"]
    # And no money moved.
    assert _balance(client) == "0.00"


def test_webhook_unknown_payout_is_404(client, john):
    r = client.post("/webhooks/payouts/nope", json={"status": "failed"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_webhook_invalid_status_is_422(client, payout_id):
    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "exploded"})
    assert r.status_code == 422  # Pydantic rejects it before the service runs


# --------------------------------------------------------------------------
# Stage 5.4: the whole assignment in one test, over HTTP.
# --------------------------------------------------------------------------


def test_q2_full_cycle_over_http(client, payout_id):
    """The complete Q2 story through the public API.

    A rate-limited user's payout fails; they must be able to withdraw again
    immediately. The 429 assertions in the middle prove the second withdrawal
    succeeds *because of* the failure handling, not because the rate limit
    was never in play.
    """
    # The 24h slot is occupied by the initiated payout...
    r = client.post("/users/john_doe/withdrawals", json={"amount": 1.00})
    assert r.status_code == 429
    assert "retry_after" in r.json()
    assert int(r.headers["Retry-After"]) > 0

    # ...the gateway fails the payout...
    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})
    assert r.status_code == 200
    assert _balance(client) == "68.00"

    # ...and the same user can now withdraw again, immediately.
    r = client.post("/users/john_doe/withdrawals", json={"amount": 68.00})
    assert r.status_code == 201
    retry_id = r.json()["id"]
    assert retry_id != payout_id
    assert _balance(client) == "0.00"

    # The retry completes; nothing comes back this time.
    client.post(f"/webhooks/payouts/{retry_id}", json={"status": "processing"})
    r = client.post(f"/webhooks/payouts/{retry_id}", json={"status": "completed"})
    assert r.status_code == 200
    assert _balance(client) == "0.00"

    # Final audit trail: the ledger tells the entire story in order.
    types = [e["entry_type"] for e in client.get("/users/john_doe/ledger").json()]
    assert types == [
        "REJECTION_ADJUSTMENT",   # sale rejected:        -4
        "FINAL_CREDIT",           # sale approved:       +36
        "FINAL_CREDIT",           # sale approved:       +36
        "WITHDRAWAL_DEBIT",       # first withdrawal:    -68
        "WITHDRAWAL_REVERSAL",    # gateway failure:     +68
        "WITHDRAWAL_DEBIT",       # retry withdrawal:    -68
    ]
