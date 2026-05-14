"""Audit filename entropy regression tests.

Defence-in-depth follow-up to PR #120 (audit-file IDOR fix). The
PR #120 ownership check is the primary defence; unguessable
filenames are the secondary defence — if the check ever regresses
silently, attackers still can't guess valid filenames from public
information about the target.

Threat model: an attacker who knows the target's website domain
(public info) and roughly when an audit was run could previously
construct the exact filename:

    enfactum-com_full_20260506_145038

Both pieces — slug + minute-resolution timestamp — are derivable
from external observation (e.g., they post about running an audit,
or you scan the changes on their site). With the random nonce
appended, the attacker would need to brute-force ~64 bits of
entropy per attempt.

These tests guarantee:
1. The filename includes a sufficiently-large random suffix.
2. Two filenames generated within the same second are distinct.
3. The filename uses only URL-safe characters (no escaping, no
   filesystem surprises).
4. The legacy filename shape (without the nonce) no longer matches
   what we generate — a regression that strips the nonce would be
   caught immediately.
"""
import re

from save_results import build_base_filename


def test_filename_includes_random_nonce():
    """The filename must include enough entropy to resist guessing
    from public info (website + approximate timestamp)."""
    fn = build_base_filename("https://example.com", "full")
    # Format: <slug>_<type>_YYYYMMDD_HHMMSS_<nonce>. token_urlsafe(8) can
    # itself contain '_' or '-', so a plain rsplit on '_' would split
    # mid-nonce on ~17% of runs (flake). Anchor on the timestamp instead.
    m = re.search(r"_\d{8}_\d{6}_(.+)$", fn)
    assert m, f"Filename {fn!r} doesn't match <slug>_<type>_<ts>_<nonce> shape"
    nonce = m.group(1)
    # secrets.token_urlsafe(8) yields ~11 chars (base64-ish encoding
    # of 8 bytes). Anything shorter than ~10 chars is a regression.
    assert len(nonce) >= 10, (
        f"Nonce {nonce!r} is too short ({len(nonce)} chars) — should be "
        f"~11 from secrets.token_urlsafe(8). Regression?"
    )


def test_filenames_are_unique_within_same_second():
    """Two audits generated in quick succession (same timestamp
    second) must still produce different filenames — otherwise the
    second one would overwrite the first on disk."""
    fns = {build_base_filename("https://example.com", "full") for _ in range(50)}
    assert len(fns) == 50, (
        f"Expected 50 unique filenames, got {len(fns)} — collision means "
        f"the random nonce isn't being added or has insufficient entropy."
    )


def test_filename_is_url_safe():
    """Filename slots into /audit/<filename> without URL escaping AND
    into S3 keys without surprises."""
    fn = build_base_filename("https://example.com", "quick")
    # URL-safe chars per RFC 3986 unreserved + "_": A-Za-z0-9_-.
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", fn), (
        f"Filename {fn!r} contains a character that needs URL-escaping."
    )


def test_filename_keeps_readable_prefix():
    """Ops/debugging still benefit from the website + audit_type +
    timestamp prefix — only the suffix should be opaque."""
    fn = build_base_filename("https://enfactum.com", "full")
    assert fn.startswith("enfactum_com_full_"), (
        f"Filename prefix changed unexpectedly: {fn!r}"
    )
    # And the timestamp shape should still be there.
    assert re.search(r"_\d{8}_\d{6}_", fn), (
        f"Timestamp shape (_YYYYMMDD_HHMMSS_) missing from {fn!r}"
    )


def test_filename_does_not_match_legacy_shape():
    """The legacy filename (no nonce) is a `<slug>_<type>_<timestamp>`
    triple — exactly 3 trailing underscore-separated segments. The
    new shape has 4. If someone strips the nonce in a future PR, this
    test catches it."""
    fn = build_base_filename("https://example.com", "full")
    # New shape has at least 4 underscore-separated trailing segments
    # (slug parts may have underscores too — count from the right).
    legacy_pattern = re.compile(r"^[a-z0-9_]+_(full|quick)_\d{8}_\d{6}$")
    assert not legacy_pattern.fullmatch(fn), (
        f"Filename {fn!r} matches the LEGACY shape — the random nonce "
        f"defence has been removed. See PR #120 for context."
    )
