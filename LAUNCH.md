# Launch runbook

End-to-end procedure for taking DarInsights from dev to live prod.
Treat as a checklist — work top to bottom, no skipping. Designed
to be done in **one sitting** once your domain is bought, with
**~45 min** of focused time.

> # ⏰ RECURRING OPS — DO NOT MISS
> **Next `SECRET_KEY` rotation due: `2026-11-01`** (then every ~6 mo).
> This file is passive — it cannot notify you. **Right now, put
> `2026-11-01` and `2027-05-01` "rotate SECRET_KEY — see LAUNCH.md"
> in your phone/calendar as real alerts.** Full instructions and the
> running tick-list are in [Operational reminders](#operational-reminders)
> at the bottom of this runbook. (An automated reminder was attempted
> but the scheduler is unavailable in this environment — the calendar
> entry is the real safety net.)

## Why this exists

Going partially live is the most dangerous state. Common failure modes
this runbook prevents:

- `STRIPE_SECRET_KEY=sk_live_…` swapped but `STRIPE_PRICE_…` IDs still
  point at test products → checkout 500s for every customer.
- Live `sk_live_…` swapped but `STRIPE_WEBHOOK_SECRET` still on test →
  payments succeed silently but no credits granted, no plan upgrade.
- `RESEND_FROM` still pointing at `@resend.dev` sandbox → verification
  emails only deliver to the account owner, all real users locked out
  of paid features.
- `SECRET_KEY` left at the placeholder string → session cookies
  forgeable by anyone with access to the source.

The boot-time `_check_launch_config()` in `app.py` warns about most
of these on every startup until they're fixed — your sanity guard.

---

## Phase 0 — Pre-flight (do BEFORE the cutover sitting)

Have these in front of you in a doc/notepad:

- [ ] **Production domain** (e.g. `app.darinsights.com`) with DNS
      pointing at your host (Vercel / Render / Fly / Railway / EC2).
- [ ] **Live Stripe products created** in Stripe Dashboard → Live
      mode toggle → Products. Create:
      - Pro plan ($29/mo recurring)
      - Growth plan ($79/mo recurring)
      - Extra Workspace add-on ($9/mo recurring)
      - Extra Seat add-on ($5/mo recurring)
      - 6 credit bundles (5/20/50 credits × free-tier-price /
        subscriber-price; see `services/stripe_helper.py:32-44`)
      - Copy the **10 `price_live_…` IDs** into your notepad.
- [ ] **Resend domain verified.** Resend dashboard → Domains →
      Add `yourdomain.com`. Wait for green check ("Verified") —
      DNS records can take 30 min to propagate.
- [ ] **`RESEND_API_KEY`** for the live Resend account.
- [ ] **Decided your secret-storage method** (Vercel/Render env
      tab, Fly secrets, AWS SSM, `.env` on server, etc.).
- [ ] **Test card handy** (you'll be your own first paying user;
      refund yourself after the verification step).

> **You can do Phase 0 now even without a domain.** Stripe products
> can be pre-created in Live mode; the price IDs don't need a
> domain to exist.

---

## Phase 1 — Generate domain-independent secrets (5 min)

```bash
# A real signing key — replaces the placeholder
python -c 'import secrets; print("SECRET_KEY=" + secrets.token_urlsafe(48))'

# Cron secret if you have any /cron/* endpoints in scheduled jobs
python -c 'import secrets; print("CRON_SECRET=" + secrets.token_urlsafe(32))'
```

Stash both in your secrets manager / `.env`.

---

## Phase 2 — Set up the live Stripe webhook (10 min)

In Stripe Dashboard:

1. Switch to **Live mode** (toggle, top right of the dashboard).
2. Developers → Webhooks → **+ Add endpoint**.
3. URL: `https://YOUR-DOMAIN/stripe/webhook`
4. **Events to send** (must match exactly what `stripe_webhook()`
   handles in `app.py:8658`):
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
   - `invoice.payment_succeeded`
5. Click "Add endpoint", then click "Reveal" on the signing
   secret → copy `whsec_live_…`.

> If you skip this, every live webhook is rejected (400 "invalid
> signature") → users pay but never get credits or a plan upgrade.
> This is the single most expensive cutover mistake.

---

## Phase 3 — Update OAuth redirect URIs to the live domain (10 min)

If you use any of these integrations, add the live URL to each
provider's allowed-redirects list. **Don't delete the localhost
entries** — keep both so dev stays working.

| Provider | Console | Redirect to add |
|---|---|---|
| Google OAuth | console.cloud.google.com → APIs → Credentials | `https://YOUR-DOMAIN/auth/google/callback` |
| Shopify OAuth | partners.shopify.com → your app → Configuration | `https://YOUR-DOMAIN/integrations/shopify/callback` |
| Google Search Console | same Google Cloud project (separate redirect) | `https://YOUR-DOMAIN/integrations/gsc/callback` |

---

## Phase 4 — Set production env vars (10 min)

In your host's env-var UI (Vercel / Render / Fly / etc.), set
**all of these together** in one save:

```env
# --- Flask runtime ---
FLASK_ENV=production
SECRET_KEY=<from Phase 1>
DATABASE_URL=<your prod Postgres URL>

# --- Stripe (all 12 must flip together) ---
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_live_...     # from Phase 2
STRIPE_PRICE_PLAN_PRO=price_live_...
STRIPE_PRICE_PLAN_GROWTH=price_live_...
STRIPE_PRICE_EXTRA_WORKSPACE=price_live_...
STRIPE_PRICE_EXTRA_SEAT=price_live_...
STRIPE_PRICE_BUNDLE_5_FREE=price_live_...
STRIPE_PRICE_BUNDLE_20_FREE=price_live_...
STRIPE_PRICE_BUNDLE_50_FREE=price_live_...
STRIPE_PRICE_BUNDLE_5_SUB=price_live_...
STRIPE_PRICE_BUNDLE_20_SUB=price_live_...
STRIPE_PRICE_BUNDLE_50_SUB=price_live_...

# --- Resend ---
RESEND_API_KEY=<live key from Phase 0>
RESEND_FROM=DarInsights <noreply@YOUR-DOMAIN>   # NOT @resend.dev

# --- Recommended for multi-instance prod ---
RATELIMIT_STORAGE_URI=redis://...               # else per-IP rate
                                                # limits drift across
                                                # replicas (PR #133)
SENTRY_DSN=https://...@sentry.io/...            # error monitoring;
                                                # unset = 500s only
                                                # in stdout logs

# --- Recommended: CSP violation reporting ---
CSP_REPORT_URI=https://...                      # else violation reports
                                                # only land in browser
                                                # consoles (no telemetry)

# --- Clear any leftover dev placeholders ---
# Make sure SHOPIFY_REDIRECT_URI is unset OR set to your live URL
# Make sure S3_ENDPOINT_URL is unset OR pointing at a real bucket
```

### What each env var does

| Env var | Purpose | What breaks if wrong |
|---|---|---|
| `FLASK_ENV` | Triggers cookie `Secure` flag, HSTS header, debugger off | No `Secure` on cookies → MITM cookie theft on plain HTTP |
| `SECRET_KEY` | Signs session cookies | Forgeable sessions if placeholder; users randomly logged out if rotated |
| `STRIPE_SECRET_KEY` | Stripe API auth | All payment routes 500 |
| `STRIPE_WEBHOOK_SECRET` | Verifies webhook signatures | Live webhooks 400-rejected → silent failure |
| `STRIPE_PRICE_*` | Maps plans/bundles to live products | Checkout fails for that specific product |
| `RESEND_API_KEY` + `RESEND_FROM` | Transactional email | Verification / reset emails not delivered |
| `RATELIMIT_STORAGE_URI` | Shared rate-limit counters | Multi-instance: attackers route around limits |
| `CSP_REPORT_URI` | CSP violation collector endpoint | Without it, violation reports only hit the browser console — no telemetry to act on |
| `SENTRY_DSN` | Sends unhandled exceptions to Sentry | 500s only land in stdout logs; you won't notice live errors |

---

## Phase 5 — Deploy and check the boot logs (5 min)

Trigger the deploy. Watch the boot logs for the launch-config
check (added in `app.py` `_check_launch_config()`):

```
======================================================================
LAUNCH CONFIG WARNINGS — fix before going live:
======================================================================
```

**If you see ZERO warnings, you're correctly configured.** Any
warnings = you missed an env var. Fix and redeploy before doing
the verification step — debugging from a partial config is harder
than just fixing it now.

Sanity-check the actual response:

```bash
curl -I https://YOUR-DOMAIN/login
```

Expected response headers (from `_apply_security_headers`):

```
HTTP/1.1 200 OK
Server: DarInsights                                # NOT "Werkzeug/X.Y"
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
Referrer-Policy: strict-origin-when-cross-origin
Strict-Transport-Security: max-age=31536000; includeSubDomains
Content-Security-Policy-Report-Only: default-src 'self'; ...
Set-Cookie: session=…; HttpOnly; SameSite=Lax; Secure; Path=/
```

If you don't see `Secure` on the cookie, `FLASK_ENV` isn't set
to `production` — fix before continuing.

---

## Phase 6 — End-to-end verification with a real card (10 min)

Use Stripe Dashboard → Live mode → Payments to watch each
transaction land. Refund yourself in Stripe after each test.

| # | Step | Pass criteria |
|---|---|---|
| 1 | `GET https://YOUR-DOMAIN/` | Loads landing page |
| 2 | Sign up a fresh real email (use a real address, not `+test`) | Verification email arrives in real inbox (NOT sandbox) |
| 3 | Click verification link | Redirects to dashboard, "verify your email" banner gone |
| 4 | Create workspace via `/clients/new` | Lands on `/client/<slug>/brand-context` (PR #108 fix) |
| 5 | Subscribe to Pro with a real card | Stripe Dashboard → Webhooks → Recent deliveries shows **HTTP 200** for `checkout.session.completed` |
| 6 | Check the user's wallet | Balance went from 3 → 78 (3 starter + 75 monthly Pro) |
| 7 | Check the user row | `plan='pro'`, `payment_status='ok'` |
| 8 | Buy a 20-credit bundle | Wallet balance increments by 20, webhook delivery 200 |
| 9 | Open Stripe Customer Portal, cancel subscription | `customer.subscription.deleted` webhook fires → user's plan flips back to `free` |
| 10 | Refund both transactions in Stripe Dashboard | Refund succeeds; user's wallet does NOT auto-decrement (intentional) |

If all 10 pass → you are live.

---

## Phase 7 — Quick smoke-test of the security-critical paths (5 min)

```bash
# Public landing
curl -I https://YOUR-DOMAIN/                  # 200

# Login page
curl -I https://YOUR-DOMAIN/login             # 200, has security headers

# Anon access to protected JSON API
curl -I https://YOUR-DOMAIN/api/wallet        # 302 → /login (NOT 200)

# Webhook signature rejection
curl -X POST https://YOUR-DOMAIN/stripe/webhook \
  -H "Content-Type: application/json" \
  -d '{}'                                     # 400 "invalid_signature"
```

Open browser → Settings → trigger a password reset to your own
email. Email arrives at production address (not sandbox).

---

## Rollback if something breaks

Keep the test-mode env values in your notepad. Recovery sequence:

1. Revert env vars in host UI to test-mode values.
2. Trigger redeploy.
3. If users got partway through paying:
   - Refund them via Stripe Dashboard.
   - Email each one an apology.
   - Manually grant credits via `/admin` (admin user → user detail
     page → grant credits form).

---

## Things to do AFTER launch (not blockers)

These don't block ship but are worth doing in week 1:

- [ ] **Sentry / error monitoring** connected so you see 500s as
      they happen. The `_apply_security_headers` already strips the
      `Server` header so scanners can't fingerprint your stack —
      Sentry tells you what's actually breaking.
- [ ] **CSP cutover from report-only to enforcing.** PR #141
      shipped CSP in report-only mode. After a few days of telemetry,
      flip the header name in `app.py` (`Content-Security-Policy-
      Report-Only` → `Content-Security-Policy`) and delete the
      sentinel test in `tests/test_security_headers.py::
      test_csp_not_yet_in_enforcing_mode`.
- [ ] **GDPR UI forms** wired into `/settings/account` template.
      The endpoints exist (PR #149: `/settings/account/delete` +
      `/settings/account/export-data`) — they just need form HTML.
- [ ] **Audit log review** for past IDOR exploitation. PR #120
      fixed the audit-file IDOR. Worth checking access logs for
      requests like `GET /audit/<filename>` where the requesting
      user's ID didn't match the audit file's `user_id`. Anyone
      who scraped data before the fix won't show up post-fix.
- [ ] **Add CSP `report-uri`** pointing at a violation collector
      (Sentry has one, or self-host with `csp-violation-handler`).
      Without it, violation reports go to the browser console only.
- [ ] **Regenerate `SECRET_KEY` periodically** (every 6-12 months).
      All sessions are invalidated on rotation — annoying for users,
      but limits the blast radius of a hypothetical key compromise.

---

## Operational reminders

> ### 🔴 NEXT ACTION DUE: `2026-11-01` — rotate `SECRET_KEY`
> A markdown file can't page you. **If you have not already put this
> in your phone/calendar as a real alert, stop and do it now** —
> title it "rotate SECRET_KEY — LAUNCH.md", set it for `2026-11-01`,
> and add a second for `2027-05-01`. That external alert is the only
> thing that will actually reach you on the day; everything below is
> just the how-to once it does.

Calendar fallback for the recurring ops items above. Tick off each
date as you do the rotation, then pencil in the next one (+6 months)
**and add the new calendar alert immediately** — don't rely on
remembering to re-read this file.

- [ ] **2026-11-01** — rotate `SECRET_KEY` (see Phase 1 for the
      regen command). Rotation invalidates all active sessions —
      log everyone out — so do it in a low-traffic window. The
      new value goes in your host's env var store (Render / Heroku
      / whatever), NOT in the repo. ➜ After doing it, tick this box
      and set the next calendar alert for 2027-05-01.
- [ ] **2027-05-01** — rotate `SECRET_KEY` (next cadence tick).
      ➜ After doing it, set the next alert for ~2027-11-01.

---

## Reference: env-var checklist

Use this as the final eyeball pass before you click "save" on the
host's env vars. Every line should have a real value (no `your_…`
placeholders, no `sk_test_…` for the production env).

```env
# Required
FLASK_ENV=production
SECRET_KEY=<48-char random>
DATABASE_URL=<your prod Postgres>

# Stripe (12 vars — all together or none)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_live_...
STRIPE_PRICE_PLAN_PRO=price_live_...
STRIPE_PRICE_PLAN_GROWTH=price_live_...
STRIPE_PRICE_EXTRA_WORKSPACE=price_live_...
STRIPE_PRICE_EXTRA_SEAT=price_live_...
STRIPE_PRICE_BUNDLE_5_FREE=price_live_...
STRIPE_PRICE_BUNDLE_20_FREE=price_live_...
STRIPE_PRICE_BUNDLE_50_FREE=price_live_...
STRIPE_PRICE_BUNDLE_5_SUB=price_live_...
STRIPE_PRICE_BUNDLE_20_SUB=price_live_...
STRIPE_PRICE_BUNDLE_50_SUB=price_live_...

# Email
RESEND_API_KEY=<live key>
RESEND_FROM=DarInsights <noreply@YOUR-DOMAIN>

# AI / search
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...                   # if used

# OAuth (only if using these integrations)
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=https://YOUR-DOMAIN/auth/google/callback
SHOPIFY_API_KEY=...
SHOPIFY_API_SECRET=...

# Storage (S3 or compatible — only if you want logos in the cloud)
S3_BUCKET=...
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_REGION=...

# Recommended for multi-instance prod
RATELIMIT_STORAGE_URI=redis://...

# Recommended: CSP violation reporting
CSP_REPORT_URI=https://...                # Sentry CSP endpoint or
                                          # self-hosted collector

# Recommended — error monitoring (sentry.io project DSN)
SENTRY_DSN=https://...@sentry.io/...

# Cron (only if you have scheduled jobs)
CRON_SECRET=<32-char random>
```

That's the whole cutover. The codebase is ready — this is purely
an env-var + Stripe-dashboard exercise once your domain is live.
