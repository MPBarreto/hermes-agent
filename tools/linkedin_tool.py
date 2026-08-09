"""LinkedIn outbound tool — invitations, acceptance checks, and DMs.

Drives a persistent Chrome profile with Playwright. LinkedIn has no public
API for connections or messaging, so a logged-in browser session is the only
actuator available.

Division of responsibility: this tool clicks, HubSpot remembers. The handler
receives explicit profile URLs and reports what happened per profile; the
agent reads the funnel from HubSpot (contacts in the LinkedIn Funnel stage,
keyed on ``hs_lead_status``) and writes the new state back. That keeps the
tool testable without a network and usable when the HubSpot MCP is down.

Operational notes worth knowing before calling it:

* The daily caps (5 invitations, 5 messages) are enforced here, in the
  ledger — not left to the model to count.
* Every action opens a **visible** Chromium window on the host machine.
  Headless changes the browser fingerprint and is what trips LinkedIn's
  checkpoint, so it exists only for tests.
* One process at a time. Concurrent calls get ``busy`` rather than
  corrupting the shared profile.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from tools.registry import registry

logger = logging.getLogger(__name__)

# Batch ceiling, set by the dispatch timeout rather than by preference.
# `registry.dispatch` bridges async handlers through `_run_async`, which
# enforces `future.result(timeout=300)` whenever an event loop is already
# running (gateway and cron).
#
# Human pacing (tools/linkedin/humanize.py) costs ~12-20s of reading and
# clicking per profile plus a right-skewed gap that averages ~30s and
# occasionally takes a multi-minute break. Simulated over 6000 runs, the
# share of batches that blow the 300s ceiling is:
#
#     2 profiles -> 0%      4 profiles -> 19%
#     3 profiles -> 5%      5 profiles -> 38%
#
# So three is the ceiling. Two calls of 3 cover the 5/day cap, and a timeout
# mid-batch is harmless anyway: the ledger is written per profile, so the
# retry skips whoever already got an invitation.
MAX_PROFILES_PER_CALL = 3

_PROFILES_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": MAX_PROFILES_PER_CALL,
    "description": (
        f"LinkedIn profile URLs (max {MAX_PROFILES_PER_CALL} per call). "
        "Accepts any common form; they are canonicalized internally."
    ),
}

_SCHEMA = {
    "name": "linkedin",
    "description": (
        "LinkedIn outbound automation against a persistent, manually logged-in "
        "Chrome profile. Actions: 'login' opens a browser for interactive "
        "sign-in and returns immediately; 'login_status' reports whether the "
        "saved session is still valid; 'connect' sends connection invitations "
        "(always without a note); 'check_accepted' reports which pending "
        "invitations have been accepted; 'message' sends a DM to existing "
        "1st-degree connections. Daily caps (5 invitations, 5 messages) and "
        "same-day deduplication are enforced automatically. Opens a visible "
        "browser window on the host machine."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "login",
                    "login_status",
                    "connect",
                    "check_accepted",
                    "message",
                    "status",
                    "mark_synced",
                ],
                "description": (
                    "'status' reports remaining daily quota and pending HubSpot "
                    "syncs without opening a browser. 'mark_synced' clears rows "
                    "from that queue after the HubSpot write succeeded — call it "
                    "with the same profiles you just updated, or the queue never "
                    "drains."
                ),
            },
            "profiles": _PROFILES_SCHEMA,
            "template": {
                "type": "string",
                "description": (
                    "Template name for action='message' (file stem under "
                    "~/.hermes/linkedin/templates/). Mutually exclusive with 'text'."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Literal message body, used instead of 'template'. Supports "
                    "{first_name} / {company} placeholders."
                ),
            },
            "values": {
                "type": "object",
                "description": (
                    "Placeholder values per profile URL, e.g. "
                    '{"https://...": {"first_name": "Ana", "company": "Acme"}}. '
                    "Missing placeholders render empty rather than failing."
                ),
                "additionalProperties": True,
            },
            "ledger_action": {
                "type": "string",
                "enum": ["connect", "message", "check_accepted"],
                "description": (
                    "Optional narrowing for action='mark_synced'. Omit it and "
                    "every queue is cleared for the given profiles, which is "
                    "almost always what you want."
                ),
            },
        },
        "required": ["action"],
    },
}


def check_linkedin_requirements() -> bool:
    """Expose the toolset only when Playwright is importable.

    Deliberately does not probe for the Chromium binary: that check spans the
    filesystem and this runs on every schema build (cached 30s). A missing
    browser surfaces as an actionable message at call time instead.
    """
    from tools.linkedin.browser import playwright_available

    return playwright_available()


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "message": message, **extra}, ensure_ascii=False)


def _normalize_profiles(raw: Any, limit: int | None = MAX_PROFILES_PER_CALL) -> tuple[list[str], str | None]:
    """Validate and canonicalize the profile list.

    ``limit=None`` skips the batch cap, which only exists to keep
    browser-driving actions inside the dispatch timeout.
    """
    from tools.linkedin.utils import normalize_linkedin_url

    if not isinstance(raw, list) or not raw:
        return [], "Provide 'profiles' as a non-empty list of LinkedIn profile URLs."
    if limit is not None and len(raw) > limit:
        return [], (
            f"Too many profiles ({len(raw)}). This action accepts at most "
            f"{limit} per call — split the batch and call again."
        )

    seen: list[str] = []
    for item in raw:
        url = normalize_linkedin_url(str(item))
        if "/in/" not in url:
            return [], f"Not a LinkedIn profile URL: {item!r}"
        if url not in seen:
            seen.append(url)
    return seen, None


async def _handle(args: dict, **_kwargs) -> str:
    action = (args.get("action") or "").strip()

    from tools.linkedin import ledger
    from tools.linkedin.browser import (
        BrowserUnavailable,
        LinkedInBlockedError,
        LinkedInLoginRequired,
    )
    from tools.linkedin.lock import ProfileBusy

    if action == "status":
        return json.dumps(
            {
                "status": "ok",
                "date": ledger.local_today(),
                "remaining_today": {
                    name: ledger.remaining_today(name) for name in ledger.QUOTA_ACTIONS
                },
                "pending_hubspot_sync": ledger.pending_sync(),
            },
            ensure_ascii=False,
        )

    if action == "mark_synced":
        # No browser, no timeout pressure — the batch cap does not apply.
        profiles, err = _normalize_profiles(args.get("profiles"), limit=None)
        if err:
            return _error(err)

        # Default to draining every queue rather than guessing one. The old
        # default of "connect" silently no-opped after a DM — the caller had
        # no way to know a `ledger_action` parameter even existed — so the
        # message queue could never empty. Clearing an already-synced row is
        # a no-op, so sweeping all of them is safe.
        requested = (args.get("ledger_action") or "").strip()
        targets = [requested] if requested else list(ledger.QUOTA_ACTIONS) + ["check_accepted"]

        cleared: list[str] = []
        for profile in profiles:
            # Evaluate every queue, not `any(...)` — that short-circuits and
            # would leave a second pending row behind for the same profile.
            hits = [ledger.mark_synced(name, profile) for name in targets]
            if any(hits):
                cleared.append(profile)

        return json.dumps(
            {
                "status": "ok",
                "action": "mark_synced",
                "ledger_actions": targets,
                "cleared": cleared,
                "not_found": [p for p in profiles if p not in cleared],
                "still_pending": len(ledger.pending_sync()),
            },
            ensure_ascii=False,
        )

    if action == "login":
        from tools.linkedin.session import spawn_login

        return json.dumps(spawn_login(), ensure_ascii=False)

    # Everything below drives a browser and needs the profile lock.
    # Arguments are validated before the action module is imported, so bad
    # input always yields a usable message.
    if action not in ("login_status", "connect", "check_accepted", "message"):
        return _error(f"Unknown action: {action!r}")

    profiles: list[str] = []
    if action != "login_status":
        profiles, err = _normalize_profiles(args.get("profiles"))
        if err:
            return _error(err)

    template = args.get("template")
    text = args.get("text")
    if action == "message" and not template and not text:
        return _error("action='message' requires either 'template' or 'text'.")

    from tools.linkedin import actions

    def _bind() -> Callable:
        if action == "login_status":
            return lambda: actions.login_status()
        if action == "connect":
            return lambda: actions.connect(profiles)
        if action == "check_accepted":
            return lambda: actions.check_accepted(profiles)
        values = args.get("values") if isinstance(args.get("values"), dict) else {}
        return lambda: actions.message(profiles, template=template, text=text, values=values)

    try:
        result = await _bind()()
        return json.dumps(result, ensure_ascii=False)
    except ProfileBusy as exc:
        return json.dumps({"status": "busy", "message": str(exc)}, ensure_ascii=False)
    except LinkedInLoginRequired as exc:
        return json.dumps({"status": "login_required", "message": str(exc)}, ensure_ascii=False)
    except LinkedInBlockedError as exc:
        return json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False)
    except BrowserUnavailable as exc:
        return json.dumps({"status": "unavailable", "message": str(exc)}, ensure_ascii=False)


registry.register(
    name="linkedin",
    toolset="linkedin",
    schema=_SCHEMA,
    handler=_handle,
    check_fn=check_linkedin_requirements,
    is_async=True,
    description="LinkedIn outbound: login, connection invitations, acceptance checks, DMs",
    emoji="🔗",
)
