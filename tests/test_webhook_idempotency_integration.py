"""End-to-end Stripe webhook idempotency tests.

The DB-level uniqueness constraint on WebhookEvent.event_id is already
covered by tests/test_stripe_webhook.py. This file covers the
DISPATCHER behaviour: when a webhook with a duplicate event_id hits
/stripe/webhook, does the handler:

  - Skip the per-event handler (no double-process)?
  - Return 200 with {"ignored": "duplicate"}?
  - Race-handle a concurrent worker (IntegrityError → "duplicate_race")?
  - Return 200 even when the handler RAISES (poison-event protection)?
  - Return 200 (not 500) for unknown event types (forward-compat)?

Why it matters: Stripe replays webhooks aggressively on any non-2xx
or timeout. If dedup is wired wrong, a single payment can grant
credits twice, or a subscription update fires twice and double-runs
the side effects. Most fintech bugs in production are exactly this
shape — silent, only visible on the customer's bank statement.

The signature check is mocked out (we're testing the dispatcher,
not stripe_helper). For the signature path itself see
tests/test_auth_boundaries.py::test_stripe_webhook_*.
"""
import json
from unittest.mock import patch

import pytest

# Import the module itself (not the symbols) so patch.object can
# reliably swap the per-event handler used by the dispatcher.
# `patch("app._handle_checkout_completed", ...)` was non-deterministic
# under full-suite runs vs. isolation — the string-path resolution
# sometimes missed the bare-name lookup inside stripe_webhook().
import app as app_module
from app import app as flask_app, db, WebhookEvent


@pytest.fixture
def client(app_ctx):
    return flask_app.test_client()


def _post_event(client, *, event_id, event_type, data=None):
    """POST a synthetic Stripe event to /stripe/webhook with the
    signature check mocked out so we test the dispatcher path."""
    fake_event = {
        "id": event_id,
        "type": event_type,
        "data": {"object": data or {}},
    }
    with patch("services.stripe_helper.construct_webhook_event",
               return_value=fake_event):
        return client.post(
            "/stripe/webhook",
            data=json.dumps(fake_event),
            content_type="application/json",
            headers={"Stripe-Signature": "t=1,v1=mock"},
        )


def test_first_event_processes_and_records(client):
    """Baseline: a fresh event_id should be processed and recorded."""
    resp = _post_event(
        client, event_id="evt_first",
        event_type="checkout.session.completed",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "ignored" not in body  # not a duplicate

    # WebhookEvent row should exist
    row = WebhookEvent.query.filter_by(event_id="evt_first").one_or_none()
    assert row is not None
    assert row.event_type == "checkout.session.completed"


def test_duplicate_event_id_is_skipped(client):
    """The CRITICAL test — a replayed event must NOT re-run the
    handler. Returns 200 with ignored=duplicate."""
    _post_event(client, event_id="evt_dup",
                event_type="checkout.session.completed")
    # Replay the exact same event_id — a Stripe retry, basically.
    resp = _post_event(client, event_id="evt_dup",
                       event_type="checkout.session.completed")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("ignored") == "duplicate", (
        f"Replay was NOT recognised as duplicate: {body!r}"
    )

    # Critically: only ONE row in WebhookEvent for this id.
    rows = WebhookEvent.query.filter_by(event_id="evt_dup").all()
    assert len(rows) == 1, (
        f"Expected 1 WebhookEvent row for replayed event, found {len(rows)}"
    )


# Note: the concurrent-insert race path (two workers see no existing
# row, both try to insert, second hits IntegrityError) is hard to
# simulate cleanly via mocks — the DB-level guarantee is already
# covered in test_stripe_webhook.py::TestWebhookEventIdempotency::
# test_unique_event_id_rejects_duplicate. The dispatcher catches
# the IntegrityError at app.py:8726-8734 and returns
# {"ignored": "duplicate_race"}.


def test_unknown_event_type_returns_200_not_500(client):
    """Forward-compat: a brand-new Stripe event type the app doesn't
    know about must NOT crash. Should record as 'ignored' and 200.
    Otherwise Stripe retries forever and pollutes our error logs."""
    resp = _post_event(client, event_id="evt_unknown",
                       event_type="payment_intent.amount_capturable_updated")
    assert resp.status_code == 200
    row = WebhookEvent.query.filter_by(event_id="evt_unknown").one_or_none()
    assert row is not None
    assert row.status == "ignored"
    assert "Unhandled event_type" in (row.notes or "")


def test_handler_exception_returns_200_marks_failed(client):
    """Poison-event protection: if the per-event handler raises, we
    must return 200 (so Stripe stops retrying) and mark the row as
    'failed' so an admin can clear it after fixing the bug.

    Otherwise: Stripe retries the broken event every few minutes
    forever, our error logs flood, and the duplicate-suppression
    check never kicks in (because the row's never committed)."""
    # Patch the handler to explode. patch.object on the module ref is
    # more deterministic than patch("app._handle_checkout_completed")
    # — the string path occasionally missed the bare-name lookup
    # inside stripe_webhook() under full-suite runs.
    with patch.object(app_module, "_handle_checkout_completed",
                      side_effect=RuntimeError("simulated handler crash")):
        resp = _post_event(client, event_id="evt_poison",
                           event_type="checkout.session.completed")

    assert resp.status_code == 200, (
        f"Handler exception leaked as {resp.status_code} — Stripe will "
        f"retry this event forever"
    )
    row = WebhookEvent.query.filter_by(event_id="evt_poison").one_or_none()
    assert row is not None
    assert row.status == "failed"
    assert "RuntimeError" in (row.notes or "")


def test_replay_after_failure_is_still_skipped(client):
    """If a poison event was recorded as 'failed', a Stripe retry
    must STILL be deduped — otherwise we re-trigger the same broken
    handler and either succeed unexpectedly (wrong outcome attached
    to the same event_id) or just log the same error again forever."""
    with patch.object(app_module, "_handle_checkout_completed",
                      side_effect=RuntimeError("crash")):
        _post_event(client, event_id="evt_poison_replay",
                    event_type="checkout.session.completed")

    # Stripe replays the exact same event_id. Dispatcher must dedupe
    # even though the first attempt failed.
    with patch.object(app_module, "_handle_checkout_completed") as handler:
        resp = _post_event(client, event_id="evt_poison_replay",
                           event_type="checkout.session.completed")
        assert handler.call_count == 0, (
            "Replay re-ran the handler — dedup not honoured for "
            "previously-failed events. Will loop forever."
        )

    body = resp.get_json()
    assert body.get("ignored") == "duplicate"
    assert body.get("previous_status") == "failed"


def test_invalid_signature_returns_400(client):
    """Sanity check the rejection path — bogus signature gets 400,
    NOT processed. Pairs with test_auth_boundaries.py but exercises
    the path with the real (unmocked) construct_webhook_event."""
    # Don't patch — let the real signature check fire.
    resp = client.post(
        "/stripe/webhook", data="{}",
        content_type="application/json",
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    # In the test env STRIPE_WEBHOOK_SECRET may be missing → 503.
    # In prod with a key it'd be 400. Both are non-2xx rejections.
    assert resp.status_code in (400, 503), resp.status_code
    # And nothing was recorded.
    assert WebhookEvent.query.count() == 0
