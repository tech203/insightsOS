"""
Lightweight homepage scan.

The audit's downstream LLM calls (query generation, business profile
research, action engine) all rely on knowing what the brand actually
sells. Trusting the user-typed `industry` field is fragile — it can
be wrong, generic, or conflate related brands. Tavily research can
drift if multiple businesses share a name.

`scan_website()` solves that by fetching the homepage directly and
extracting the unambiguous signals: title, meta description, OG tags,
JSON-LD, the visible hero text, and the menu/nav categories. That
becomes the ground truth the LLM gets pinned to.

Pure stdlib + requests — no headless browser, no JS execution. For
a JS-only SPA we'd miss content, but the major confectionery /
ecommerce / SMB sites all server-render enough markup for this to
catch the brand identity.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_SCRIPT_BLOCK = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_META = re.compile(
    r'<meta\s+(?:name|property)=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']',
    re.I,
)
_JSONLD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


class _NavExtractor(HTMLParser):
    """Pull text out of <nav> / role=navigation / header menu blocks
    so we can detect product categories without parsing the entire
    document."""

    def __init__(self):
        super().__init__()
        self._in_nav = 0
        self._in_skip = False
        self.items: List[str] = []
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        attr_d = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._in_skip = True
            return
        role = (attr_d.get("role") or "").lower()
        cls = (attr_d.get("class") or "").lower()
        if tag == "nav" or role == "navigation" or "menu" in cls or "nav" in cls:
            self._in_nav += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._in_skip = False
            return
        if tag == "nav":
            self._in_nav = max(0, self._in_nav - 1)
        if tag in ("a", "li", "button") and self._in_nav and self._buf:
            text = " ".join(self._buf).strip()
            text = _WS.sub(" ", text)
            if 1 < len(text) < 60 and text.lower() not in {
                "open menu", "close menu", "back", "skip to content",
                "login", "account", "cart", "0",
            }:
                self.items.append(text)
            self._buf = []

    def handle_data(self, data):
        if self._in_skip or not self._in_nav:
            return
        text = data.strip()
        if text:
            self._buf.append(text)


def _fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch a URL and return raw HTML, or None on any failure."""
    if not url or not url.strip():
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:
        logger.warning("site_scan fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code >= 400:
        logger.warning("site_scan got %s for %s", resp.status_code, url)
        return None
    return resp.text


def _extract_visible_text(html: str, max_chars: int = 1500) -> str:
    """Strip HTML to get a contiguous block of visible text. We use
    this to pull the brand's homepage tagline / hero copy."""
    cleaned = _SCRIPT_BLOCK.sub(" ", html)
    text = _TAG.sub(" ", cleaned)
    text = _WS.sub(" ", text).strip()
    return text[:max_chars]


def _extract_jsonld(html: str) -> List[Dict[str, Any]]:
    """All inline JSON-LD blocks parsed into Python dicts. Skip
    invalid ones silently."""
    out: List[Dict[str, Any]] = []
    for blob in _JSONLD.findall(html):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def _extract_meta(html: str) -> Dict[str, str]:
    """Title + meta description + OG/Twitter tags."""
    meta: Dict[str, str] = {}
    title_match = _TITLE.search(html)
    if title_match:
        meta["title"] = title_match.group(1).strip()
    for key, value in _META.findall(html):
        meta[key.lower()] = value.strip()
    return meta


def _extract_nav_categories(html: str) -> List[str]:
    parser = _NavExtractor()
    try:
        parser.feed(html)
    except Exception:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for item in parser.items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 20:
            break
    return out


def scan_website(url: str) -> Dict[str, Any]:
    """Fetch a homepage and return the unambiguous signals about
    what the brand sells.

    Output shape — every field defaults to None / [] when fetch fails:
      {
        "url":              str final URL,
        "title":            str <title> content,
        "description":      str meta description (or og:description),
        "og_title":         str,
        "og_description":   str,
        "hero_text":        str first ~1500 chars of visible body text,
        "categories":       [str] menu / nav items detected,
        "jsonld":           [dict] parsed JSON-LD blocks,
        "fetched":          bool whether the fetch succeeded,
      }
    """
    blank = {
        "url": url,
        "title": None,
        "description": None,
        "og_title": None,
        "og_description": None,
        "hero_text": None,
        "categories": [],
        "jsonld": [],
        "fetched": False,
    }

    html = _fetch_html(url)
    if not html:
        return blank

    meta = _extract_meta(html)
    description = (
        meta.get("description")
        or meta.get("og:description")
        or meta.get("twitter:description")
    )
    og_title = meta.get("og:title")
    title = meta.get("title")

    return {
        "url": url,
        "title": title,
        "description": description,
        "og_title": og_title,
        "og_description": meta.get("og:description"),
        "hero_text": _extract_visible_text(html),
        "categories": _extract_nav_categories(html),
        "jsonld": _extract_jsonld(html),
        "fetched": True,
    }


def brand_context_blurb(scan: Dict[str, Any]) -> str:
    """One-paragraph summary of what the homepage scan turned up.
    Used as grounding context for downstream LLM calls so the model
    knows what the brand actually sells before generating queries
    or summaries."""
    parts: List[str] = []
    title = (scan.get("title") or "").strip()
    if title:
        parts.append(f"Site title: {title}.")
    desc = (scan.get("description") or "").strip()
    if desc:
        parts.append(f"Description: {desc}")
    og_desc = (scan.get("og_description") or "").strip()
    if og_desc and og_desc != desc:
        parts.append(f"OpenGraph description: {og_desc}")
    cats = scan.get("categories") or []
    if cats:
        parts.append(f"Detected nav categories: {', '.join(cats[:10])}.")
    hero = (scan.get("hero_text") or "").strip()
    if hero:
        parts.append(f"Homepage hero copy (first 600 chars): {hero[:600]}")
    return " ".join(parts).strip()
