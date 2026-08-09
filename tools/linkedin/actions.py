"""The browser-driving actions behind the `linkedin` tool.

Each public coroutine acquires the profile lock, opens the persistent
Chromium profile, and returns a plain dict the tool layer serializes.

Three rules hold across every action here, all of them learned the hard way
by the project this was ported from:

* **Check for a block before touching a profile**, and abort the whole run
  when one appears. Clicking through a challenge escalates a soft check into
  a hard restriction.
* **Write each outcome to the ledger the moment it is known**, never at the
  end of a batch, so a dispatch timeout or a crash still leaves a truthful
  record of what was actually sent.
* **Never report success without positive evidence.** An unconfirmed send is
  reported as ``unclear`` and flagged for human review, because the failure
  mode of guessing is messaging the same person twice.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Optional

from tools.linkedin import humanize, ledger
from tools.linkedin.browser import (
    CONNECTIONS_URL,
    FEED_URL,
    detect_block,
    is_logged_in,
    open_page,
    raise_if_blocked,
)
from tools.linkedin.lock import profile_lock
from tools.linkedin.selectors import (
    CONFIRM_SENT_SCRIPT,
    CONNECT_BUTTON_SELECTORS,
    CONNECTIONS_CARD_SCRIPT,
    CONNECTIONS_CLICK_MESSAGE_SCRIPT,
    CONNECTIONS_SCRAPE_SCRIPT,
    CONNECTIONS_SEARCH_SELECTORS,
    CONVERSATION_PILL_SELECTORS,
    FORCE_CLICK_SCRIPT,
    INSERT_TEXT_SCRIPT,
    MESSAGE_BOX_SELECTORS,
    MESSAGE_BUTTON_SCRIPT,
    MESSAGE_EVIDENCE_SCRIPT,
    MORE_BUTTON_SELECTORS,
    OVERFLOW_MENU_MARKERS,
    OVERFLOW_MENU_SCOPES,
    PENDING_HINTS,
    PROFILE_ACTION_SCOPES,
    PROFILE_DEGREE_SCRIPT,
    SEND_BUTTON_SELECTORS,
    SEND_WITHOUT_NOTE_SELECTORS,
)
from tools.linkedin.utils import (
    extract_first_name,
    first_visible,
    looks_non_first_degree,
    normalize_linkedin_url,
    normalize_person_name,
    profile_slug,
    render_template,
    save_artifacts,
)

logger = logging.getLogger(__name__)

# Pacing lives in tools/linkedin/humanize.py. These names are kept because
# tests and callers reference them, but the real cadence is the right-skewed
# distribution in ``humanize.human_gap`` — a uniform draw over a fixed range
# is itself a bot signature, however wide the range.
DELAY_MIN_SECONDS = humanize.PROFILE_GAP_MIN
DELAY_MAX_SECONDS = humanize.PROFILE_GAP_MAX


class MessageConfirmationUnclear(RuntimeError):
    """The message may or may not have been delivered.

    Distinct from a failure: a failure is safe to retry, this is not.
    """


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

async def login_status() -> dict:
    """Report whether the saved session still works.

    Runs headless because it only reads state; skipping the visible window
    keeps it cheap enough to use as a precondition check.
    """
    with profile_lock("login_status"):
        async with open_page(headless=True, require_login=False) as page:
            await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=45000)
            await humanize.settle(page, 2000)

            if await detect_block(page):
                return {
                    "status": "blocked",
                    "message": (
                        "LinkedIn is showing a captcha or checkpoint. Run "
                        "action='login' and clear it by hand."
                    ),
                }
            if await is_logged_in(page):
                return {"status": "valid", "message": "LinkedIn session is active."}
            return {
                "status": "expired",
                "message": (
                    "No active LinkedIn session. Run action='login', sign in, "
                    "then check again."
                ),
            }


# ---------------------------------------------------------------------------
# Connection invitations
# ---------------------------------------------------------------------------

async def _profile_scope(page):
    """Return a locator for the profile's own top card.

    Everything on a profile page is a trap for an unscoped query: the
    "More profiles for you" sidebar carries other people's Connect buttons,
    and the activity feed below carries a "···" menu per post — clicking one
    navigates away to that post.

    LinkedIn now ships obfuscated class names (no ``pv-top-card``, no
    ``ph5``, no ``artdeco-card``), so the card is located structurally
    instead: find the profile name, then walk up to the ancestor that also
    holds the action buttons. That anchor survives class churn.
    """
    # Tag the card in the DOM, then hand back a normal Locator for it —
    # ElementHandle has no .locator(), and callers need to query inside.
    marked = await page.evaluate(
        """() => {
            const MARK = 'data-hermes-profile-card';
            document.querySelectorAll('[' + MARK + ']')
                .forEach((n) => n.removeAttribute(MARK));

            const main = document.querySelector('main') || document.body;
            // h1 is not dependable — LinkedIn ships profile layouts without
            // one — so fall back to the first line of the top card.
            const anchor = main.querySelector('h1') || main.querySelector('section');
            if (!anchor) return false;

            // The owner's own action button is the reliable landmark: its
            // aria-label names this profile ("Seguir Tiago Alvisi",
            // "Convidar Tiago Alvisi para se conectar"). Post menus in the
            // activity feed never carry that.
            const name = (anchor.innerText || '').trim().split('\\n')[0].trim();
            if (!name) return false;

            const owned = Array.from(
                main.querySelectorAll('button, a[role="button"], [role="button"]')
            ).filter((b) => {
                const label = b.getAttribute('aria-label') || '';
                if (!label.includes(name)) return false;
                // Exclude post-level controls that also name the author.
                return !/publica|post|coment/i.test(label);
            });
            if (!owned.length) return false;

            // Walk up from that button to the smallest block that also holds
            // the profile name. Bounded by text length: the activity feed is
            // an order of magnitude larger than the card, so a container that
            // grew past ~2000 chars has swallowed it and must be rejected —
            // that is what made a post's "···" look like the profile's.
            let node = owned[0];
            let best = null;
            for (let i = 0; i < 14 && node && node !== main; i += 1) {
                node = node.parentElement;
                if (!node) break;
                const text = node.innerText || '';
                if (text.length > 2000) break;
                if (text.includes(name)) best = node;
            }
            if (!best) return false;
            best.setAttribute(MARK, '1');
            return true;
        }"""
    )
    if marked:
        return page.locator("[data-hermes-profile-card]").first

    for selector in PROFILE_ACTION_SCOPES:
        scope = page.locator(selector).first
        try:
            if await scope.count():
                return scope
        except Exception:
            continue
    return page


async def _belongs_to_profile(control, owner: str) -> bool:
    """True when a Connect control targets *owner* rather than someone else.

    LinkedIn labels these "Convidar <name> para se conectar" / "Invite <name>
    to connect". When a name is present and it is not the profile owner, the
    control belongs to an embedded card and must not be clicked.
    """
    try:
        label = (await control.get_attribute("aria-label")) or ""
    except Exception:
        label = ""

    normalized_label = normalize_person_name(label)
    # A bare "Conectar"/"Connect" label carries no name to contradict, and
    # unlabelled controls only appear in the profile's own action bar (the
    # embedded cards always name their target).
    if not normalized_label or normalized_label in {"conectar", "connect"}:
        return True

    # The label names someone. Without a known owner there is nothing to
    # check it against, so refuse: clicking sends an invitation, and an
    # invitation to the wrong person cannot be taken back.
    if not owner:
        return False
    return normalize_person_name(owner) in normalized_label


async def _profile_owner(page) -> str:
    """Display name from the profile header, used to validate controls.

    ``h1`` is tried first but is not dependable: LinkedIn ships profile
    layouts where the name is not an ``h1`` at all. The fallback reads the
    first line of the top card, which is the name in every layout seen so
    far.
    """
    for selector in ("main h1", "h1"):
        try:
            node = page.locator(selector).first
            if await node.count():
                text = (await node.inner_text(timeout=2000)).strip()
                if text:
                    return text
        except Exception:
            continue

    try:
        card = page.locator("main section:first-of-type").first
        if await card.count():
            text = (await card.inner_text(timeout=2000)).strip()
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            return first_line
    except Exception:
        pass
    return ""


async def _open_overflow_menu(page, scope):
    """Click the profile's "···" button and return the opened menu.

    Returns None when no menu opened. Opening is verified by looking for
    items that exist only in this menu ("Salvar como PDF", "Denunciar/
    bloquear"): without that check a failed click looks identical to a menu
    with no Connect item, and the caller would fall back to a document-wide
    search that finds a stranger's button.
    """
    more = await first_visible(scope, MORE_BUTTON_SELECTORS)
    if more is None:
        return None

    await _activate(more)
    await humanize.settle(page, 900)

    for selector in OVERFLOW_MENU_SCOPES:
        menu = page.locator(selector).last
        try:
            if not await menu.count() or not await menu.is_visible(timeout=1000):
                continue
            text = await menu.inner_text(timeout=1500)
        except Exception:
            continue
        if any(marker.lower() in text.lower() for marker in OVERFLOW_MENU_MARKERS):
            return menu
    return None


async def _find_connect_button(page, owner: str = ""):
    """Locate the Connect control belonging to this profile.

    Two places to look, both scoped. The profile card is checked first; when
    LinkedIn promotes "Seguir"/"Enviar mensagem" it moves Connect into the
    "···" overflow menu, which is checked second.

    Neither search may run against the whole document: the sidebar and the
    activity feed carry other people's Connect buttons, and they come first
    in DOM order. That is how the first live run picked up
    "Convidar Caroline Savioli para se conectar" on Tiago Alvisi's profile.
    """
    scope = await _profile_scope(page)

    direct = await first_visible(scope, CONNECT_BUTTON_SELECTORS)
    if direct is not None and await _belongs_to_profile(direct, owner):
        return direct

    menu = await _open_overflow_menu(page, scope)
    if menu is not None:
        # Inside the menu the item is a bare "Conectar" with no name, so the
        # menu scope — not the label — is what proves it targets this profile.
        candidate = await first_visible(menu, CONNECT_BUTTON_SELECTORS, timeout=2000)
        if candidate is not None and await _belongs_to_profile(candidate, owner):
            return candidate
    return None


async def _activate(control) -> None:
    """Click, escalating through scroll and a raw DOM click.

    LinkedIn's sticky header intercepts pointer events often enough that a
    plain click is not reliable on its own.
    """
    try:
        await control.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await control.click(timeout=6000)
        return
    except Exception:
        pass
    await control.evaluate(FORCE_CLICK_SCRIPT)


async def _confirm_pending(page) -> bool:
    """True when the profile now shows an invitation as pending."""
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    lowered = body.lower()
    return any(hint.lower() in lowered for hint in PENDING_HINTS)


async def _connect_one(page, profile: str) -> dict:
    """Send one invitation. Returns a per-profile result dict."""
    await page.goto(profile, wait_until="domcontentloaded", timeout=45000)
    await humanize.settle(page, 2500)
    await raise_if_blocked(page)  # aborts the whole run

    # Read the profile before acting on it. Clicking Connect milliseconds
    # after load, with no scroll and no pointer movement, is one of the
    # easiest automation signals for LinkedIn to pick up.
    await humanize.before_action(page)

    owner = await _profile_owner(page)
    button = await _find_connect_button(page, owner)
    if button is None:
        # Already connected, already invited, or a follow-only profile where
        # LinkedIn promotes "Follow" and offers no Connect action at all.
        return {
            "profile": profile,
            "result": "skipped",
            "reason": "connect_button_not_found",
            "owner": owner,
        }

    await _activate(button)
    await humanize.settle(page, 1000)

    send = await first_visible(page, SEND_WITHOUT_NOTE_SELECTORS, timeout=5000)
    if send is None:
        artifacts = await save_artifacts(page, profile, "connect-modal", "send_without_note_not_found")
        return {
            "profile": profile,
            "result": "failed",
            "reason": "send_without_note_not_found",
            **artifacts,
        }

    await send.click()
    await humanize.settle(page, 1500)

    if not await _confirm_pending(page):
        artifacts = await save_artifacts(page, profile, "connect-confirm", "no_pending_evidence")
        return {
            "profile": profile,
            "result": "failed",
            "reason": "no_pending_confirmation",
            **artifacts,
        }
    return {"profile": profile, "result": "ok"}


async def connect(profiles: list[str]) -> dict:
    """Send connection invitations, always without a note.

    Notes are skipped deliberately: free accounts get a small monthly quota
    of them and spending it does not measurably lift acceptance.
    """
    results: list[dict] = []
    quota = ledger.remaining_today("connect")

    with profile_lock("connect"):
        async with open_page() as page:
            for index, profile in enumerate(profiles):
                if quota <= 0:
                    results.append(
                        {"profile": profile, "result": "skipped", "reason": "daily_limit_reached"}
                    )
                    continue
                if ledger.already_done_today("connect", profile):
                    results.append(
                        {"profile": profile, "result": "skipped", "reason": "already_requested_today"}
                    )
                    continue

                try:
                    outcome = await _connect_one(page, profile)
                except Exception as exc:
                    if type(exc).__name__ == "LinkedInBlockedError":
                        raise
                    outcome = {"profile": profile, "result": "failed", "reason": str(exc)[:300]}

                # Recorded immediately, before the delay, so a timeout mid-batch
                # cannot erase work already done.
                ledger.append(
                    "connect",
                    profile,
                    outcome["result"] if outcome["result"] == "ok" else f"{outcome['result']}:{outcome.get('reason','')}"[:120],
                    ledger.SYNC_PENDING if outcome["result"] == "ok" else ledger.SYNC_NA,
                )
                if outcome["result"] == "ok":
                    quota -= 1
                results.append(outcome)

                if index < len(profiles) - 1 and quota > 0:
                    await humanize.between_profiles(index, len(profiles))

    sent = sum(1 for r in results if r["result"] == "ok")
    return {
        "status": "ok",
        "action": "connect",
        "requested": sent,
        "remaining_today": ledger.remaining_today("connect"),
        "results": results,
        "next_step": (
            "For each result with result='ok', set the HubSpot contact's "
            "hs_lead_status to OPEN_DEAL ('Conexão Solicitada')."
            if sent
            else "Nothing to sync."
        ),
    }


# ---------------------------------------------------------------------------
# Acceptance detection
# ---------------------------------------------------------------------------

async def _scrape_connections(page) -> list[dict]:
    """Bulk-read the connections list.

    One page load answers "who accepted?" for the whole batch, instead of a
    profile visit per lead — far fewer requests, far less rate-limit risk.
    """
    await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded", timeout=45000)
    await humanize.settle(page, 2500)
    await raise_if_blocked(page)

    # Scroll to load the lazy list. Irregular distances and pauses rather
    # than four identical 2200px jumps, which no trackpad or wheel produces.
    for _ in range(random.randint(4, 6)):
        await page.mouse.wheel(0, random.randint(1400, 2600))
        await humanize.settle(page, random.randint(700, 1500))

    raw = await page.evaluate(CONNECTIONS_SCRAPE_SCRIPT)
    rows: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        rows.append(
            {
                "name": name,
                "name_norm": normalize_person_name(name),
                "profile_url": normalize_linkedin_url(str(item.get("rawHref") or "")),
                "has_message_button": bool(item.get("hasMessageButton")),
            }
        )
    return rows


def match_connection_rows(profiles: list[str], rows: list[dict]) -> dict[str, dict]:
    """Map profile URL -> matching connections row.

    Matches on the ``/in/<slug>`` identifier, because the two sides arrive in
    different shapes: connections cards expose relative hrefs while HubSpot
    stores absolute URLs. Comparing whole strings silently matches nothing.

    Name matching is deliberately not attempted. The source project needed it
    because it scraped names off search pages with no URL; here every profile
    arrives as a URL, so falling back to names would only add ambiguity.

    A slug matching more than one row is reported as ambiguous rather than
    guessed — mislabeling someone as connected is what triggers an InMail
    charge downstream.
    """
    matched: dict[str, dict] = {}
    for profile in profiles:
        target = profile_slug(profile)
        if not target:
            continue
        hits = [row for row in rows if profile_slug(row.get("profile_url", "")) == target]
        if len(hits) == 1:
            matched[profile] = {**hits[0], "match_reason": "profile_slug"}
        elif len(hits) > 1:
            matched[profile] = {"ambiguous": True, "match_reason": "slug_multiple_rows"}
    return matched


async def _profile_degree(page) -> str:
    """Return "1", "2", "3", or "" when the degree badge can't be read.

    Reads the badge adjacent to the profile name rather than scanning the
    page for degree markers: recommendations and "people also viewed" carry
    other people's badges, and matching one of those misreports an accepted
    connection as still pending.
    """
    try:
        info = await page.evaluate(PROFILE_DEGREE_SCRIPT)
    except Exception:
        return ""
    return str(info.get("degree") or "") if isinstance(info, dict) else ""


async def _profile_shows_message_button(page, profile: str) -> bool:
    """Per-profile fallback: does this profile look like a 1st-degree connection?

    A Message control alone is not proof. LinkedIn shows "Enviar mensagem" on
    profiles that merely allow open messaging — creator and follow-only
    accounts do this while still sitting at 2nd degree, and the composer it
    opens is a paid InMail. Observed on /in/tiago-alvisi (2026-08-07), which
    renders "Seguir" + "Enviar mensagem" with a "• 2º" badge.

    So the degree badge wins: if the card says 2nd or 3rd, the answer is no
    regardless of which buttons are on screen.
    """
    await page.goto(profile, wait_until="domcontentloaded", timeout=45000)
    await humanize.settle(page, 2500)
    await raise_if_blocked(page)

    degree = await _profile_degree(page)
    if degree in {"2", "3"}:
        return False
    if degree == "1":
        return True  # the badge is the authoritative signal

    # Degree unreadable — fall back to the Message control, which only a
    # connection gets (Premium/InMail variants are filtered out in the script).
    evidence = await page.evaluate(MESSAGE_EVIDENCE_SCRIPT)
    return bool(isinstance(evidence, dict) and evidence.get("found"))


async def check_accepted(profiles: list[str]) -> dict:
    """Report which pending invitations have been accepted.

    Two stages, cheap first: bulk-scrape the connections page, then visit
    only the profiles that stayed unresolved.
    """
    results: list[dict] = []

    with profile_lock("check_accepted"):
        async with open_page() as page:
            rows = await _scrape_connections(page)
            matched = match_connection_rows(profiles, rows)

            unresolved: list[str] = []
            for profile in profiles:
                hit = matched.get(profile)
                if hit is None:
                    unresolved.append(profile)
                elif hit.get("ambiguous"):
                    results.append(
                        {
                            "profile": profile,
                            "result": "ambiguous",
                            "reason": hit["match_reason"],
                        }
                    )
                elif hit.get("has_message_button"):
                    results.append(
                        {
                            "profile": profile,
                            "result": "accepted",
                            "name": hit.get("name", ""),
                            "source": "connections_page",
                        }
                    )
                else:
                    unresolved.append(profile)

            for index, profile in enumerate(unresolved):
                try:
                    accepted = await _profile_shows_message_button(page, profile)
                except Exception as exc:
                    if type(exc).__name__ == "LinkedInBlockedError":
                        raise
                    results.append(
                        {"profile": profile, "result": "failed", "reason": str(exc)[:300]}
                    )
                    continue
                results.append(
                    {
                        "profile": profile,
                        "result": "accepted" if accepted else "pending",
                        "source": "profile_page",
                    }
                )
                if index < len(unresolved) - 1:
                    await humanize.between_profiles(index, len(unresolved))

    for entry in results:
        if entry["result"] == "accepted":
            ledger.append("check_accepted", entry["profile"], "ok", ledger.SYNC_PENDING)

    accepted_count = sum(1 for r in results if r["result"] == "accepted")
    return {
        "status": "ok",
        "action": "check_accepted",
        "accepted": accepted_count,
        "results": results,
        "next_step": (
            "For each result='accepted', set hs_lead_status to CONNECTED "
            "('Linkedin Conectado')."
            if accepted_count
            else "No new acceptances."
        ),
    }


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------

async def _open_thread_from_connections(page, profile: str, name_hint: str) -> tuple[bool, str]:
    """Open a conversation from the connections page.

    Preferred over the profile page: a card here only exposes a Message
    control for an actual connection, so it cannot silently open a paid
    InMail composer.
    """
    await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded", timeout=45000)
    await humanize.settle(page, 2000)
    await raise_if_blocked(page)

    if name_hint:
        search = await first_visible(page, CONNECTIONS_SEARCH_SELECTORS, timeout=4000)
        if search is not None:
            try:
                # Typed, not filled: the search box fires an autocomplete
                # request per keystroke, so a name that materializes whole
                # produces a request pattern no human generates.
                await humanize.type_like_human(search, name_hint)
                await humanize.settle(page, 1800)
            except Exception:
                try:
                    await search.fill(name_hint, timeout=4000)
                    await humanize.settle(page, 1800)
                except Exception:
                    pass

    cards = await page.evaluate(CONNECTIONS_CARD_SCRIPT, "")
    if not isinstance(cards, list):
        return False, "connections_cards_not_found"

    target = profile_slug(profile)
    hits = [
        card
        for card in cards
        if isinstance(card, dict)
        and profile_slug(str(card.get("rawHref") or "")) == target
        and card.get("hasMessageButton")
    ]
    if not hits:
        return False, "not_found_in_connections"
    if len(hits) > 1:
        return False, "ambiguous_connections_match"

    clicked = await page.evaluate(
        CONNECTIONS_CLICK_MESSAGE_SCRIPT,
        {"profileUrl": target, "name": str(hits[0].get("name") or "")},
    )
    if not (isinstance(clicked, dict) and clicked.get("clicked")):
        return False, "message_click_failed"

    await humanize.settle(page, 1800)
    return True, "connections_page"


async def _open_thread_from_profile(page, profile: str) -> tuple[bool, str]:
    """Profile-page fallback, refused for non-1st-degree contacts.

    On a 2nd/3rd-degree profile the Message button opens a paid InMail
    composer. The degree badge is checked before clicking anything.
    """
    await page.goto(profile, wait_until="domcontentloaded", timeout=45000)
    await humanize.settle(page, 2500)
    await raise_if_blocked(page)

    # Read the badge next to the name. Scanning the page for "2º" catches
    # recommendations and "people also viewed", which would block a
    # legitimate send to a real connection.
    if await _profile_degree(page) in {"2", "3"}:
        return False, "blocked_non_first_degree"

    clicked = await page.evaluate(MESSAGE_BUTTON_SCRIPT)
    if not (isinstance(clicked, dict) and clicked.get("clicked")):
        return False, "message_button_not_found"

    await humanize.settle(page, 1800)
    return True, "profile_page"


async def _find_in_frames(page, selectors: list[str], timeout: int = 4000, last: bool = False):
    """Find a control in any frame, main document first.

    The conversation overlay lives in an iframe, so a plain page-level query
    silently misses the composer and the Send button.
    """
    for frame in [page.main_frame] + [f for f in page.frames if f is not page.main_frame]:
        try:
            found = await first_visible(frame, selectors, timeout=timeout, last=last)
        except Exception:
            continue
        if found is not None:
            return found
    return None


async def _expand_conversation(page) -> None:
    """Expand the chat overlay if it opened minimized.

    Clicking "Enviar mensagem" sometimes restores a previously collapsed
    conversation instead of opening a fresh one, leaving a pill in the
    bottom-right corner. While collapsed the composer is absent from the
    DOM entirely, so typing and confirmation both fail with no obvious
    cause. Observed 2026-08-07.
    """
    if await _find_in_frames(page, MESSAGE_BOX_SELECTORS, timeout=1500) is not None:
        return  # already expanded

    for selector in CONVERSATION_PILL_SELECTORS:
        pill = page.locator(selector).last
        try:
            if not await pill.count() or not await pill.is_visible(timeout=800):
                continue
            await pill.click(timeout=3000)
            await humanize.settle(page, 1200)
            if await _find_in_frames(page, MESSAGE_BOX_SELECTORS, timeout=2500) is not None:
                return
        except Exception:
            continue


async def _insert_and_send(page, text: str) -> None:
    """Type the message and press Send."""
    await _expand_conversation(page)

    box = await _find_in_frames(page, MESSAGE_BOX_SELECTORS, timeout=6000)
    if box is None:
        raise RuntimeError("message_box_not_found")

    try:
        await box.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass

    # Type character by character. fill() drops the whole string in one
    # event, which no keyboard can produce — a 400-character DM appearing
    # instantaneously is a stronger signal than any delay between profiles.
    try:
        await humanize.type_like_human(box, text)
    except Exception:
        try:
            await box.click(timeout=5000, force=True)
            await box.fill(text, timeout=5000, force=True)
        except Exception:
            # Rich-text editors reject fill(); drive the DOM directly.
            await box.evaluate(INSERT_TEXT_SCRIPT, text)
    await humanize.settle(page, 800)

    # A beat before sending — people re-read what they wrote.
    await humanize.pause(random.uniform(0.8, 3.0))

    send = await _find_in_frames(page, SEND_BUTTON_SELECTORS, timeout=5000, last=True)
    if send is None:
        raise RuntimeError("send_button_not_found")
    await send.click(timeout=5000)
    await humanize.settle(page, 1800)


async def _confirm_sent(page, text: str) -> bool:
    """Poll for positive evidence the message left the compose box.

    Checks every frame, not just the main document. LinkedIn renders the
    conversation overlay inside an iframe (``/preload/?_bprMode=vanilla``),
    so ``page.evaluate`` — which only runs against the top-level document —
    never sees the composer or the sent message. That produced a permanent
    false "unclear" on every delivered DM (observed 2026-08-07): the message
    arrived, and the tool reported it needed human review.
    """
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    payload = {"fullText": text, "firstLine": first_line}

    for _ in range(8):
        for frame in page.frames:
            try:
                if await frame.evaluate(CONFIRM_SENT_SCRIPT, payload):
                    return True
            except Exception:
                continue  # detached or cross-origin frame
        await humanize.settle(page, 1000)
    return False


async def message(
    profiles: list[str],
    template: Optional[str] = None,
    text: Optional[str] = None,
    values: Optional[dict[str, Any]] = None,
) -> dict:
    """Send a DM to existing 1st-degree connections."""
    from tools.linkedin.templates import list_templates, load_template

    body_template = text
    if not body_template and template:
        body_template = load_template(template)
        if body_template is None:
            return {
                "status": "error",
                "message": f"Template {template!r} not found.",
                "available": [t["name"] for t in list_templates()],
            }

    values = values or {}
    results: list[dict] = []
    quota = ledger.remaining_today("message")

    with profile_lock("message"):
        async with open_page() as page:
            for index, profile in enumerate(profiles):
                if quota <= 0:
                    results.append(
                        {"profile": profile, "result": "skipped", "reason": "daily_limit_reached"}
                    )
                    continue
                if ledger.already_done_today("message", profile):
                    results.append(
                        {"profile": profile, "result": "skipped", "reason": "already_messaged_today"}
                    )
                    continue

                per_profile = values.get(profile) or values.get(normalize_linkedin_url(profile)) or {}
                if not isinstance(per_profile, dict):
                    per_profile = {}
                name_hint = str(per_profile.get("name") or per_profile.get("first_name") or "")
                if "first_name" not in per_profile and name_hint:
                    per_profile = {**per_profile, "first_name": extract_first_name(name_hint)}
                rendered = render_template(body_template, per_profile)

                try:
                    opened, source = await _open_thread_from_connections(page, profile, name_hint)
                    if not opened:
                        opened, source = await _open_thread_from_profile(page, profile)
                    if not opened:
                        results.append(
                            {"profile": profile, "result": "skipped", "reason": source}
                        )
                        ledger.append("message", profile, f"skipped:{source}"[:120])
                        continue

                    await _insert_and_send(page, rendered)
                    confirmed = await _confirm_sent(page, rendered)
                except Exception as exc:
                    if type(exc).__name__ == "LinkedInBlockedError":
                        raise
                    artifacts = await save_artifacts(page, profile, "message", str(exc))
                    results.append(
                        {
                            "profile": profile,
                            "result": "failed",
                            "reason": str(exc)[:300],
                            **artifacts,
                        }
                    )
                    ledger.append("message", profile, f"failed:{str(exc)[:80]}")
                    continue

                if confirmed:
                    results.append({"profile": profile, "result": "ok", "source": source})
                    ledger.append("message", profile, "ok", ledger.SYNC_PENDING)
                    quota -= 1
                else:
                    # Never counted as sent: retrying a message that did go out
                    # is worse than leaving it for a human to check.
                    artifacts = await save_artifacts(
                        page, profile, "message-unconfirmed", "no_send_evidence"
                    )
                    results.append(
                        {
                            "profile": profile,
                            "result": "unclear",
                            "reason": "no_send_confirmation",
                            "human_review_required": True,
                            **artifacts,
                        }
                    )
                    ledger.append("message", profile, "unclear:no_confirmation")

                if index < len(profiles) - 1 and quota > 0:
                    await humanize.between_profiles(index, len(profiles))

    sent = sum(1 for r in results if r["result"] == "ok")
    unclear = [r["profile"] for r in results if r["result"] == "unclear"]
    return {
        "status": "ok",
        "action": "message",
        "sent": sent,
        "remaining_today": ledger.remaining_today("message"),
        "results": results,
        "human_review_required": unclear,
        "next_step": (
            "For each result='ok', log the touch on the HubSpot contact. "
            "Verify anything listed in human_review_required by hand before resending."
            if sent or unclear
            else "Nothing sent."
        ),
    }
