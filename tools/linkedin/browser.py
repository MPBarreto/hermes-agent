"""Chromium session management for LinkedIn.

Ported from `linkedin_browser.py`, with the two behaviours that are correct
for a standalone CLI and wrong inside Hermes removed:

* The original blocked on ``context.wait_for_event("close", timeout=0)`` to
  wait for a human to log in or solve a captcha. Inside a tool handler that
  is an unconditional 300s dispatch timeout.
* The original raised ``SystemExit`` to end the CLI. ``SystemExit`` inherits
  from ``BaseException``, so it slips past the ``except Exception`` in
  ``registry.dispatch`` and would take down the gateway process.

Playwright is imported inside functions: this package is imported in every
Hermes session by ``discover_builtin_tools()``, including on machines that
have never installed the browser extra.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from tools.linkedin.paths import ensure_dirs, profile_dir

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"
CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

# URL fragments that only appear on a real interstitial. A blocked session is
# *navigated* to one of these — it is not something page copy can fake.
BLOCK_URL_HINTS = (
    "/checkpoint",
    "/challenge",
    "captcha",
    "authwall",
)

# Phrases that indicate a block when they appear in page copy. Deliberately
# specific: single words like "challenge" or "restricted" are everyday English
# that shows up in ordinary feed posts written by other people. A post reading
# "ready to tackle challenges" once halted the whole funnel (2026-08-07).
BLOCK_TEXT_HINTS = (
    "security verification",
    "verificação de segurança",
    "verificacao de seguranca",
    "temporarily restricted",
    "temporariamente restrita",
    "conta restrita",
    "unusual activity",
    "atividade incomum",
    "confirme que você não é um robô",
    "confirm you're not a robot",
    "let's do a quick security check",
    "vamos fazer uma verificação rápida",
)

# Kept for backwards compatibility with callers/tests that imported it.
BLOCK_HINTS = BLOCK_URL_HINTS + BLOCK_TEXT_HINTS

LOGGED_IN_SELECTOR = (
    "input[placeholder*='Search'], input[placeholder*='Pesquisar'], a[href*='/feed/']"
)

_LAZY_FEATURE = "browser.linkedin"


class LinkedInBlockedError(RuntimeError):
    """LinkedIn served a captcha, checkpoint, or restriction notice."""


class LinkedInLoginRequired(RuntimeError):
    """No valid session in the persistent profile."""


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is missing."""


def ensure_playwright() -> None:
    """Make sure the Playwright wheel is importable, installing on demand.

    ``prompt=False`` because there is no human to approve an install on the
    cron path.
    """
    from tools import lazy_deps

    try:
        lazy_deps.ensure(_LAZY_FEATURE, prompt=False)
    except Exception as exc:
        raise BrowserUnavailable(
            f"Playwright is unavailable: {exc}. "
            f"Install it with: pip install 'hermes-agent[linkedin]'"
        ) from exc


def playwright_available() -> bool:
    """Cheap probe for ``check_fn`` — never triggers an install."""
    try:
        from tools import lazy_deps

        return lazy_deps.is_available(_LAZY_FEATURE)
    except Exception:
        return False


def chromium_installed() -> bool:
    """True when the Chromium build this Playwright expects is present.

    ``pip install playwright`` ships no browser; the binary comes from a
    separate ``playwright install chromium``. Checking up front turns a
    confusing stack trace into an actionable message.

    The check reads the exact Chromium revision this Playwright build pins
    (from its bundled ``browsers.json``) and looks for that specific
    directory. Globbing for ``chromium*`` is not good enough: upgrading
    Playwright leaves the previous revision in the cache, and the glob
    happily matches that stale directory, reporting success for a browser
    that cannot launch.
    """
    import json
    import os
    from pathlib import Path

    try:
        import playwright
    except Exception:
        return False

    try:
        manifest = (
            Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        )
        revisions = {
            entry["revision"]
            for entry in json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
            if entry.get("name") == "chromium"
        }
    except Exception:
        return False
    if not revisions:
        return False

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        roots = [Path(override)]
    else:
        roots = [
            Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
            Path.home() / ".cache" / "ms-playwright",  # Linux
            Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",  # Windows
        ]

    return any(
        (root / f"chromium-{revision}").is_dir()
        for root in roots
        for revision in revisions
    )


CHROMIUM_HINT = (
    "Chromium is not installed for Playwright. Run: "
    "python -m playwright install chromium"
)


async def is_logged_in(page) -> bool:
    """Detect an authenticated session by probing for feed-only chrome."""
    current = page.url.lower()
    if "/login" in current or "/checkpoint" in current or "/uas/login" in current:
        return False
    try:
        await page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=5000)
        return True
    except Exception:
        return "linkedin.com/feed" in current


async def detect_block(page) -> bool:
    """True when the current page is a captcha/checkpoint/restriction.

    Called at the top of every per-profile iteration. A block aborts the
    whole run rather than the single profile: continuing to click through a
    challenge is what escalates a soft check into a hard restriction.

    Two signals, and both are deliberately narrow. The URL check is the
    reliable one — a real block navigates you to an interstitial. The text
    check only looks at page *chrome*, never at feed content: profile pages
    and the feed are full of other people's writing, and a post reading
    "ready to tackle challenges" was enough to halt an entire run before
    this was tightened (2026-08-07).
    """
    url = page.url.lower()
    if any(hint in url for hint in BLOCK_URL_HINTS):
        logger.warning("LinkedIn block detected via URL: %s", page.url)
        return True

    # A genuine interstitial replaces the app shell: no feed, no nav. If the
    # normal navigation is present, whatever matched is someone's post.
    try:
        if await page.locator("a[href*='/feed/'], input[placeholder*='Pesquisar'], input[placeholder*='Search']").count():
            return False
    except Exception:
        pass

    try:
        text = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return False
    if any(hint in text for hint in BLOCK_TEXT_HINTS):
        logger.warning("LinkedIn block detected in page body: %s", page.url)
        return True
    return False


async def raise_if_blocked(page) -> None:
    """Convert a detected block into ``LinkedInBlockedError``."""
    if await detect_block(page):
        raise LinkedInBlockedError(
            "LinkedIn served a captcha/checkpoint. The run was stopped. "
            "Resolve it with action='login', then retry."
        )


@asynccontextmanager
async def open_page(headless: bool = False, require_login: bool = True) -> AsyncIterator:
    """Open the persistent profile and yield a page.

    Always closes the context on exit — including on a block. The original
    deliberately leaked the browser so a human could finish a captcha in it;
    here the caller is an agent, and a leaked Chromium would hold the
    profile lock forever.

    Callers must already hold ``profile_lock``.
    """
    ensure_playwright()
    if not chromium_installed():
        raise BrowserUnavailable(CHROMIUM_HINT)

    ensure_dirs()
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir()),
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        try:
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            if require_login:
                await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                await raise_if_blocked(page)
                if not await is_logged_in(page):
                    raise LinkedInLoginRequired(
                        "No active LinkedIn session. Run action='login', sign in "
                        "by hand, then confirm with action='login_status'."
                    )
            yield page
        finally:
            try:
                await context.close()
            except Exception:
                pass
