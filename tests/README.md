# Tests

Pytest suite covering the billing surface introduced in PR #85: credit
reservations, Stripe webhook handlers, and plan downgrade reconciliation.

## Running

```
pip install -r requirements-dev.txt
pytest
```

The full suite finishes in ~20 seconds. Test discovery is configured in
`pytest.ini` (`testpaths = tests`).

## Layout

| File | Covers |
|---|---|
| `conftest.py` | Shared fixtures: Flask app (test mode, CSRF off), per-test DB, `user` and `make_user` factories, `logged_in_client` |
| `test_credit_reservations.py` | `reserve_credits_for` / `commit_reservation` / `release_reservation` / `sweep_expired_reservations`, including idempotency, sentinel rows for unlimited users, and team-member-spends-from-owner-wallet behavior |
| `test_stripe_webhook.py` | Per-event handlers (`_handle_checkout_completed`, `_handle_subscription_updated`, `_handle_subscription_deleted`, `_handle_payment_failed`, `_handle_payment_succeeded`), price-ID → plan-slug mapping, `WebhookEvent` unique constraint |
| `test_downgrade_plan.py` | Workspace soft-lock, addon Stripe cancel (mocked), seat-cap invite revocation, audit-log row, `reactivate_workspace` cap checks |

## Design notes

- The DB is `sqlite:///test.db` (the file in the project root). Each
  test wraps in an app context that calls `db.create_all()` on entry
  and `db.drop_all()` on exit, so every test sees a clean schema.
- Stripe is unconfigured by default; tests that exercise the Stripe-
  configured branch patch `services.stripe_helper._stripe_module`.
- `services.email_helper` is real but with no backend configured —
  `send_email()` returns `False` and the handlers fall through to
  their offline-mode flashes.

## Adding tests

Use the existing fixtures rather than creating users manually:

```python
def test_my_thing(make_user):
    pro = make_user(plan="pro", balance=50)
    free = make_user(plan="free", balance=3, email="f@x.com")
```

For HTTP-route tests, use `logged_in_client` which returns a Flask test
client with the default user already authenticated.
