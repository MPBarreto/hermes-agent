"""Pure helpers shared across the LinkedIn actions.

Ported from `linkedin_utils.py` in the linkedin-lead-bot project, plus the
two helpers that project had copy-pasted across its modules (`_first_visible`
six times, `_save_artifacts` six times) unified here.

No Playwright import at module scope — the two page-driving helpers take an
already-constructed page object and only use its duck-typed API.
"""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from datetime import datetime, timezone
from string import Formatter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tools.linkedin.paths import artifacts_dir


def utc_now_iso() -> str:
    """Timestamp for the ledger. UTC keeps ordering stable across DST."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def random_delay(min_seconds: int, max_seconds: int) -> int:
    """Sleep a random whole number of seconds and report how long.

    The jitter between profiles is the closest thing this tool has to
    anti-detection, so it is never skipped on the real send path.
    """
    seconds = random.randint(min_seconds, max_seconds)
    await asyncio.sleep(seconds)
    return seconds


def normalize_linkedin_url(url: str) -> str:
    """Canonicalize a profile URL to ``https://www.linkedin.com/in/{slug}``.

    Profile URLs reach us from HubSpot in every shape a human might paste:
    protocol-relative, bare host, regional subdomain, trailing query strings
    from search results. The ledger dedupes on this value, so normalization
    is what makes "already contacted today" reliable.
    """
    if not url:
        return ""

    raw = url.strip()
    if raw.startswith("/"):
        raw = f"https://www.linkedin.com{raw}"
    elif raw.startswith("www.linkedin.com"):
        raw = f"https://{raw}"
    elif raw.startswith("linkedin.com"):
        raw = f"https://www.{raw}"

    split = urlsplit(raw)
    if "linkedin.com" not in split.netloc.lower():
        return raw

    path = split.path.rstrip("/")
    if "/in/" in path:
        parts = [part for part in path.split("/") if part]
        try:
            idx = parts.index("in")
            path = f"/in/{parts[idx + 1]}"
        except (ValueError, IndexError):
            pass

    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def profile_slug(url: str) -> str:
    """Extract the ``/in/<slug>`` identifier from a profile URL.

    Comparing slugs rather than whole URLs is what makes matching survive the
    shapes LinkedIn actually emits: connections-page cards carry relative
    hrefs (``/in/ana``), HubSpot stores absolute ones, and either may arrive
    with a regional subdomain or tracking query. All of those share a slug.
    """
    if not url:
        return ""
    path = urlsplit(url.strip()).path if "//" in url else url.strip()
    parts = [part for part in path.split("/") if part]
    try:
        return parts[parts.index("in") + 1].lower()
    except (ValueError, IndexError):
        return ""


def extract_first_name(name: str) -> str:
    """First token of a display name — the ``{first_name}`` placeholder."""
    cleaned = " ".join((name or "").strip().split())
    return cleaned.split(" ")[0] if cleaned else ""


def render_template(template: str, values: dict[str, Any]) -> str:
    """Fill ``{placeholders}``, tolerating ones the caller didn't supply.

    Only the fields the template actually references are passed to
    ``format``, so a template mentioning ``{company}`` for a contact with no
    company renders an empty string instead of raising ``KeyError`` mid-send.
    """
    safe_values = {
        field: str(values.get(field, ""))
        for _, field, _, _ in Formatter().parse(template)
        if field
    }
    return template.format(**safe_values)


def safe_filename(value: str, fallback: str = "artifact") -> str:
    """Slugify text for use as an artifact filename."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:90] or fallback


_DEGREE_SUFFIX_RE = re.compile(r"\s*(?:[1-3](?:st|nd|rd)|[1-3]º)\s*$", re.IGNORECASE)

# Matches a 2nd/3rd-degree marker anywhere in a blob of card text. Used to
# refuse the profile-page messaging fallback: on a non-connection the
# "Message" button opens a paid InMail composer, so a careless fallback
# silently burns InMail credits.
NON_FIRST_DEGREE_RE = re.compile(r"(?:^|\s)(?:2º|3º|2nd|3rd)(?:\s|$)", re.IGNORECASE)


def normalize_person_name(value: str) -> str:
    """Fold a LinkedIn display name into a comparable key.

    LinkedIn pads names with zero-width formatting characters, non-breaking
    spaces, a bullet separator before the degree badge, and the badge itself
    ("Fulano de Tal • 2º"). Matching connections-page rows against HubSpot
    contacts by raw string fails on all of those.
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    text = re.sub(r"\s*[•·]\s*", " • ", text)
    text = " ".join(text.split()).strip()
    if "•" in text:
        text = text.split("•", 1)[0].strip()
    text = _DEGREE_SUFFIX_RE.sub("", text).strip()
    return " ".join(text.lower().split())


def looks_non_first_degree(*fields: str) -> bool:
    """True when any field carries a 2nd/3rd-degree marker."""
    return any(NON_FIRST_DEGREE_RE.search(field or "") for field in fields)


async def first_visible(page, selectors: list[str], timeout: int = 2500, last: bool = False):
    """Return the first visible locator among *selectors*, or None.

    The bot had three copies of this that quietly disagreed — the connect
    path took ``.first`` while the messaging path took ``.last`` — so the
    choice is an explicit argument here rather than a per-copy accident.
    """
    for selector in selectors:
        try:
            locator = page.locator(selector)
            locator = locator.last if last else locator.first
            if await locator.count() and await locator.is_visible(timeout=timeout):
                return locator
        except Exception:
            continue
    return None


async def save_artifacts(page, label: str, step: str, reason: str) -> dict[str, str]:
    """Capture a screenshot + HTML snapshot for post-mortem debugging.

    Best-effort by design: a page that failed hard may also fail to
    screenshot, and losing the artifact must never mask the original error.
    """
    timestamp = utc_now_iso().replace(":", "").replace("+", "z")
    stem = safe_filename(f"{label}-{step}-{timestamp}", "linkedin")
    out_dir = artifacts_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}

    try:
        screenshot_path = out_dir / f"{stem}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        result["screenshot_path"] = str(screenshot_path)
    except Exception:
        pass

    try:
        snapshot_path = out_dir / f"{stem}.html"
        html = await page.content()
        snapshot_path.write_text(
            f"<!-- step={step} reason={reason} -->\n{html}", encoding="utf-8"
        )
        result["snapshot_path"] = str(snapshot_path)
    except Exception:
        pass

    return result
