"""Webhook endpoint and full flow through the HTTP stack."""

import pytest


@pytest.fixture
def payout_id(client):
    """An initiated payout of Rs 68, set up entirely over HTTP."""
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
    assert _balance(client) == "0.00"

    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})

    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert _balance(client) == "68.00"

    types = [e["entry_type"] for e in client.get("/users/john_doe/ledger").json()]
    assert types.count("WITHDRAWAL_DEBIT") == 1
    assert types.count("WITHDRAWAL_REVERSAL") == 1


def test_webhook_redelivery_returns_200_and_credits_once(client, payout_id):
    # gateways retry until they see 2xx, so a redelivery must not be an error
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
    assert _balance(client) == "0.00"


def test_webhook_unknown_payout_is_404(client, john):
    r = client.post("/webhooks/payouts/nope", json={"status": "failed"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_webhook_invalid_status_is_422(client, payout_id):
    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "exploded"})
    assert r.status_code == 422


def test_q2_full_cycle_over_http(client, payout_id):
    """Rate-limited user -> payout fails -> can withdraw again immediately.
    The 429 in the middle shows the second withdrawal succeeds because of the
    failure handling, not because the limit was never in play."""
    r = client.post("/users/john_doe/withdrawals", json={"amount": 1.00})
    assert r.status_code == 429
    assert "retry_after" in r.json()
    assert int(r.headers["Retry-After"]) > 0

    r = client.post(f"/webhooks/payouts/{payout_id}", json={"status": "failed"})
    assert r.status_code == 200
    assert _balance(client) == "68.00"

    r = client.post("/users/john_doe/withdrawals", json={"amount": 68.00})
    assert r.status_code == 201
    retry_id = r.json()["id"]
    assert retry_id != payout_id
    assert _balance(client) == "0.00"

    client.post(f"/webhooks/payouts/{retry_id}", json={"status": "processing"})
    r = client.post(f"/webhooks/payouts/{retry_id}", json={"status": "completed"})
    assert r.status_code == 200
    assert _balance(client) == "0.00"

    types = [e["entry_type"] for e in client.get("/users/john_doe/ledger").json()]
    assert types == [
        "REJECTION_ADJUSTMENT",
        "FINAL_CREDIT",
        "FINAL_CREDIT",
        "WITHDRAWAL_DEBIT",
        "WITHDRAWAL_REVERSAL",
        "WITHDRAWAL_DEBIT",
    ]
