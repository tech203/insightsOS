"""Forensic scan for pre-#120 audit-file IDOR exploitation.

WHY THIS EXISTS
---------------
PR #120 (commit 2a9637b, 2026-05-14) closed an IDOR where any logged-in
user could fetch another tenant's audit by guessing the slug-based
filename:

    GET /audit/<summary_filename>          # customer-facing  (was leaky)
    GET /audit/<summary_filename>/pdf       # customer-facing  (was leaky)
    GET /audit/<summary_filename>/full      # admin-only via require_internal_access()
    GET /api/audit/<summary_filename>/full  # admin-only via require_internal_access()

The fix added `_audit_belongs_to_current_user()` to the two
customer-facing routes. The two `/full` routes were already gated by
`require_internal_access()` (admin / dev_unlimited only) since
2026-05-06, so they were never part of the leak.

HONEST CONCLUSION FOR *THIS* DEPLOYMENT (read before you panic)
--------------------------------------------------------------
As of the launch runbook, this app has **never been deployed to
production**. That means:

  1. There are no production access logs from before 2026-05-14 — the
     only environment the leaky code ran in was the developer's local
     machine, which is not internet-reachable.
  2. The customer-facing leaky window therefore had an audience of
     exactly one: the developer, on localhost.
  3. The `/full` routes were admin-gated the entire time.

So for the current pre-launch state there is **nothing to scan and no
exposure to remediate**. This script is delivered for two real future
uses, not because there is an incident today:

  * Post-launch peace-of-mind / due-diligence: run it once after you
    have a few weeks of real access logs to *prove* (not assume) no
    cross-tenant audit access happened.
  * Incident-response template: if a similar IDOR is ever found again,
    point this at the logs and it does the triage.

LIMITATION YOU MUST UNDERSTAND
------------------------------
Standard web-server access logs (nginx/Apache combined) record the
client IP and the URL, but NOT the authenticated session user. True
"Bob read Alice's audit" detection needs the requester identity, which
only exists in application-level logs that log `current_user.id`.

This script therefore runs in two modes:

  * --access-log    : flags every 2xx hit on a leaky route before the
                       fix date, grouped by client IP, for manual
                       triage. Coarse but log-format-agnostic.
  * --app-log       : if you have structured app logs that emit lines
                       like `user=<id> path=/audit/<file> status=200`,
                       it cross-references the requester against the
                       audit file's true owner (read from outputs/ or
                       the `audits` table) and reports only genuine
                       cross-tenant reads. Precise.

Either way the audit-file → owner map is built from the same source of
truth the app uses (`outputs/*_summary.json` `user_id` stamp, falling
back to the `audits` table if a DATABASE_URL is given).

USAGE
-----
    # Coarse pass over an nginx access log:
    python scripts/audit_idor_scan.py --access-log /var/log/nginx/access.log

    # Precise pass if you have structured app logs:
    python scripts/audit_idor_scan.py --app-log /var/log/app/app.log

    # Override the fix cutoff (defaults to the #120 commit date):
    python scripts/audit_idor_scan.py --access-log a.log --before 2026-05-14

Exit codes: 0 = no suspicious access found, 2 = suspicious rows found
(see stdout), 1 = bad invocation / unreadable inputs.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

# The #120 fix landed in commit 2a9637b on this date. Anything *before*
# this on the customer-facing routes is in the vulnerable window.
DEFAULT_FIX_CUTOFF = "2026-05-14"

OUTPUTS_FOLDER = os.environ.get("OUTPUTS_FOLDER", "outputs")

# The two routes that were actually IDOR-leaky pre-#120. The /full
# variants are intentionally excluded: they were admin-only the whole
# time (require_internal_access), so a hit there is not a tenant leak.
LEAKY_PATH_RE = re.compile(r"^/audit/(?P<fn>[^/?]+?)(?:/pdf)?/?(?:\?.*)?$")

# nginx/Apache "combined" log line. Tolerant of the common variants.
COMBINED_RE = re.compile(
    r'(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
    r'(?P<status>\d{3})\s'
)
COMBINED_TS_FMT = "%d/%b/%Y:%H:%M:%S %z"

# Structured app-log convention this script understands, e.g.:
#   2026-05-10T09:14:22Z user=42 method=GET path=/audit/foo_summary.json status=200
APP_LOG_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*)"
    r".*?\buser=(?P<user>\S+)"
    r".*?\bpath=(?P<path>/\S*)"
    r".*?\bstatus=(?P<status>\d{3})"
)


def _parse_cutoff(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_owner_map() -> dict[str, str]:
    """filename (summary or full) -> owning user_id, from outputs/."""
    owners: dict[str, str] = {}
    pattern = os.path.join(OUTPUTS_FOLDER, "*_summary.json")
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        uid = data.get("user_id") if isinstance(data, dict) else None
        base = os.path.basename(path)
        owners[base] = "UNOWNED" if uid is None else str(uid)
        # Map the matching _full.json under the same owner so /full
        # cross-checks work if app-log mode is used.
        owners[base.replace("_summary.json", "_full.json")] = owners[base]
    return owners


def _maybe_db_owner_map() -> dict[str, str]:
    """Best-effort: augment from the audits table if reachable.

    Kept optional and import-guarded so the script runs with nothing but
    the stdlib when you only have logs + the outputs/ folder.

    Schema note: the owner of an audit lives in the `audits` table,
    whose primary key is `filename` and whose `user_id` column is the
    owning tenant (see the `Audit` model in app.py). The legacy
    `saved_audits` / `summary_filename` naming was migrated away from;
    querying it would silently return nothing.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {}
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("  (sqlalchemy not installed — skipping DB owner map)", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            rows = conn.execute(
                text("SELECT filename, user_id FROM audits")
            )
            for fn, uid in rows:
                if fn:
                    out[str(fn)] = "UNOWNED" if uid is None else str(uid)
    except Exception as exc:  # pragma: no cover - depends on live DB
        print(f"  (DB owner map unavailable: {exc})", file=sys.stderr)
    return out


def scan_access_log(path: str, cutoff: datetime, owners: dict[str, str]):
    """Coarse mode: every 2xx on a leaky route before cutoff, by IP."""
    hits = defaultdict(list)
    total = matched = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = COMBINED_RE.search(line)
            if not m:
                continue
            total += 1
            pm = LEAKY_PATH_RE.match(m.group("path"))
            if not pm:
                continue
            if not (200 <= int(m.group("status")) < 300):
                continue
            try:
                ts = datetime.strptime(m.group("ts"), COMBINED_TS_FMT)
            except ValueError:
                ts = None
            if ts is not None and ts.astimezone(timezone.utc) >= cutoff:
                continue  # after the fix — safe
            matched += 1
            fn = pm.group("fn")
            hits[m.group("ip")].append(
                (m.group("ts"), fn, owners.get(fn, "unknown-file"))
            )
    return hits, total, matched


def scan_app_log(path: str, cutoff: datetime, owners: dict[str, str]):
    """Precise mode: requester user vs. true audit owner."""
    cross = []
    total = matched = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = APP_LOG_RE.search(line)
            if not m:
                continue
            total += 1
            pm = LEAKY_PATH_RE.match(m.group("path"))
            if not pm or not (200 <= int(m.group("status")) < 300):
                continue
            raw_ts = m.group("ts").replace("Z", "+00:00").replace(" ", "T")
            try:
                ts = datetime.fromisoformat(raw_ts)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
            if ts is not None and ts >= cutoff:
                continue
            fn = pm.group("fn")
            owner = owners.get(fn)
            requester = m.group("user")
            if owner and owner not in ("UNOWNED", requester):
                matched += 1
                cross.append((m.group("ts"), requester, fn, owner))
            total += 0
    return cross, total, matched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--access-log", help="nginx/Apache combined access log")
    g.add_argument("--app-log", help="structured app log (user=/path=/status=)")
    ap.add_argument(
        "--before",
        default=DEFAULT_FIX_CUTOFF,
        help=f"vulnerable-window cutoff YYYY-MM-DD (default {DEFAULT_FIX_CUTOFF})",
    )
    args = ap.parse_args(argv)

    cutoff = _parse_cutoff(args.before)
    owners = build_owner_map()
    owners.update(_maybe_db_owner_map())
    print(
        f"Owner map: {len(owners)} audit files "
        f"(source: {OUTPUTS_FOLDER}/ + optional audits table)\n"
        f"Vulnerable window: requests strictly before {args.before} UTC\n"
    )

    if args.access_log:
        if not os.path.isfile(args.access_log):
            print(f"ERROR: no such file: {args.access_log}", file=sys.stderr)
            return 1
        hits, total, matched = scan_access_log(args.access_log, cutoff, owners)
        print(f"Parsed {total} log lines; {matched} pre-fix 2xx hits on "
              f"leaky audit routes.\n")
        if not hits:
            print("RESULT: clean — no pre-#120 access to leaky audit "
                  "routes found. Nothing to remediate.")
            return 0
        print("RESULT: review the following (coarse: access logs lack the\n"
              "session user, so these are candidates, not confirmed leaks).\n"
              "Cross-check each source IP against your auth logs for the\n"
              "same window to see whose session it was:\n")
        for ip, rows in sorted(hits.items(), key=lambda kv: -len(kv[1])):
            print(f"  client {ip} — {len(rows)} request(s):")
            for ts, fn, owner in rows[:25]:
                print(f"      [{ts}] {fn}  (true owner: user {owner})")
            if len(rows) > 25:
                print(f"      ... +{len(rows) - 25} more")
        return 2

    if not os.path.isfile(args.app_log):
        print(f"ERROR: no such file: {args.app_log}", file=sys.stderr)
        return 1
    cross, total, matched = scan_app_log(args.app_log, cutoff, owners)
    print(f"Parsed {total} audit-route log lines; {matched} confirmed "
          f"cross-tenant reads.\n")
    if not cross:
        print("RESULT: clean — no requester accessed an audit they did "
              "not own in the vulnerable window.")
        return 0
    print("RESULT: CONFIRMED cross-tenant audit access — investigate:\n")
    for ts, requester, fn, owner in cross:
        print(f"  [{ts}] user {requester} read {fn} owned by user {owner}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
