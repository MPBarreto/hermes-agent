"""Tests for the LinkedIn outbound tool.

No network and no browser: the pure logic (URL canonicalization, template
rendering, the ledger's quota arithmetic, the profile lock) is exercised
directly, and the tool layer is driven through its handler with the action
modules stubbed out.

Every test that touches state points ``HERMES_HOME`` at a tmp_path, so a run
can never read or write the developer's real ledger or Chrome profile.
"""

import asyncio
import importlib
import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def linkedin_home(tmp_path, monkeypatch):
    """Point the whole package at a throwaway HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import hermes_constants
    from tools.linkedin import ledger, lock, paths, templates

    for module in (hermes_constants, paths, ledger, lock, templates):
        importlib.reload(module)
    # Rebind the reloaded paths into their consumers.
    importlib.reload(ledger)
    importlib.reload(lock)
    importlib.reload(templates)
    return tmp_path


# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/fulano",
        "https://www.linkedin.com/in/fulano/",
        "linkedin.com/in/fulano",
        "www.linkedin.com/in/fulano",
        "/in/fulano",
        "https://br.linkedin.com/in/fulano?trk=search",
        "https://www.linkedin.com/in/fulano/detail/contact-info/",
    ],
)
def test_normalize_linkedin_url_canonicalizes(raw):
    """Every shape HubSpot or LinkedIn emits folds to one canonical URL."""
    from tools.linkedin.utils import normalize_linkedin_url

    assert normalize_linkedin_url(raw) == "https://www.linkedin.com/in/fulano"


def test_normalize_linkedin_url_passes_through_non_linkedin():
    from tools.linkedin.utils import normalize_linkedin_url

    assert normalize_linkedin_url("https://example.com/x") == "https://example.com/x"
    assert normalize_linkedin_url("") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/in/ana", "ana"),
        ("https://www.linkedin.com/in/ana", "ana"),
        ("https://br.linkedin.com/in/Ana/?trk=x", "ana"),
        ("http://127.0.0.1:8899/in/ana", "ana"),
        ("https://example.com/nope", ""),
        ("", ""),
    ],
)
def test_profile_slug(raw, expected):
    """Slug matching is what lets relative card hrefs match absolute URLs."""
    from tools.linkedin.utils import profile_slug

    assert profile_slug(raw) == expected


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def test_render_template_tolerates_missing_placeholder():
    """A contact with no company must not blow up mid-send."""
    from tools.linkedin.utils import render_template

    out = render_template("Oi {first_name}, da {company}!", {"first_name": "Ana"})
    assert out == "Oi Ana, da !"


def test_render_template_ignores_unused_values():
    from tools.linkedin.utils import render_template

    assert render_template("Oi {first_name}", {"first_name": "Ana", "x": "y"}) == "Oi Ana"


def test_extract_first_name():
    from tools.linkedin.utils import extract_first_name

    assert extract_first_name("  Ana  Maria Silva ") == "Ana"
    assert extract_first_name("") == ""


# ---------------------------------------------------------------------------
# Name normalization and degree detection
# ---------------------------------------------------------------------------

def test_normalize_person_name_strips_degree_and_invisibles():
    from tools.linkedin.utils import normalize_person_name

    assert normalize_person_name("Ana Silva • 2º") == "ana silva"
    assert normalize_person_name("Ana​Silva") == "anasilva"
    assert normalize_person_name("Ana Silva") == "ana silva"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ana Silva • 2º", True),
        ("Ana Silva • 3º", True),
        ("Ana Silva • 2nd", True),
        ("Ana Silva • 3rd", True),
        ("Ana Silva • 1º", False),
        ("Ana Silva", False),
    ],
)
def test_looks_non_first_degree(text, expected):
    """Guards the InMail-credit trap on the profile-page fallback."""
    from tools.linkedin.utils import looks_non_first_degree

    assert looks_non_first_degree(text) is expected


# ---------------------------------------------------------------------------
# Ledger: quota, idempotency, timezone
# ---------------------------------------------------------------------------

def test_ledger_counts_only_successful_actions(linkedin_home):
    from tools.linkedin import ledger

    for i in range(3):
        ledger.append("connect", f"https://www.linkedin.com/in/p{i}", "ok")
    ledger.append("connect", "https://www.linkedin.com/in/pX", "failed:no_button")

    assert ledger.count_today("connect") == 3
    assert ledger.remaining_today("connect") == 2


def test_ledger_enforces_daily_limit(linkedin_home):
    from tools.linkedin import ledger

    for i in range(ledger.DAILY_LIMITS["connect"]):
        ledger.append("connect", f"https://www.linkedin.com/in/p{i}", "ok")
    assert ledger.remaining_today("connect") == 0


def test_ledger_idempotency_uses_canonical_url(linkedin_home):
    """A re-run must not re-invite someone contacted today."""
    from tools.linkedin import ledger

    ledger.append("connect", "https://www.linkedin.com/in/ana", "ok")

    assert ledger.already_done_today("connect", "linkedin.com/in/ana/") is True
    assert ledger.already_done_today("connect", "https://br.linkedin.com/in/ana?x=1") is True
    assert ledger.already_done_today("connect", "https://www.linkedin.com/in/outro") is False
    assert ledger.already_done_today("message", "https://www.linkedin.com/in/ana") is False


def test_ledger_unlimited_action_has_no_cap(linkedin_home):
    from tools.linkedin import ledger

    assert ledger.remaining_today("check_accepted") > 1000


def test_ledger_day_boundary_is_local_not_utc(linkedin_home):
    """22:00 local is already tomorrow in UTC.

    Counting by UTC date would hand out a second full batch of invitations
    every night, so the cut has to happen in America/Sao_Paulo.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from tools.linkedin import ledger
    from tools.linkedin.paths import ensure_dirs, ledger_path

    ensure_dirs()
    today = ledger.local_today()
    local_2200 = datetime.fromisoformat(f"{today}T22:00:00").replace(
        tzinfo=ZoneInfo("America/Sao_Paulo")
    )
    stamp = local_2200.astimezone(ZoneInfo("UTC")).isoformat()
    assert stamp[:10] != today, "fixture must straddle the UTC date line"

    path = ledger_path(today[:7])
    path.write_text(ledger._HEADER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        for i in range(5):
            handle.write(
                f"| {stamp} | connect | https://www.linkedin.com/in/n{i} | ok | hubspot:synced |\n"
            )

    assert ledger.count_today("connect") == 5
    assert ledger.remaining_today("connect") == 0


def test_mark_synced_drains_the_queue(linkedin_home):
    """Without this the queue never empties and contacts get rewritten."""
    from tools.linkedin import ledger

    ledger.append("connect", "https://www.linkedin.com/in/ana", "ok", ledger.SYNC_PENDING)
    ledger.append("connect", "https://www.linkedin.com/in/bruno", "ok", ledger.SYNC_PENDING)
    assert len(ledger.pending_sync()) == 2

    assert ledger.mark_synced("connect", "linkedin.com/in/ana/") is True
    remaining = ledger.pending_sync()
    assert len(remaining) == 1
    assert remaining[0]["profile"].endswith("/bruno")


def test_mark_synced_is_idempotent_and_scoped(linkedin_home):
    from tools.linkedin import ledger

    ledger.append("connect", "https://www.linkedin.com/in/ana", "ok", ledger.SYNC_PENDING)

    assert ledger.mark_synced("connect", "https://www.linkedin.com/in/ana") is True
    # Already synced — nothing left to flip.
    assert ledger.mark_synced("connect", "https://www.linkedin.com/in/ana") is False
    # Wrong action, and an unknown profile, must not touch anything.
    assert ledger.mark_synced("message", "https://www.linkedin.com/in/ana") is False
    assert ledger.mark_synced("connect", "https://www.linkedin.com/in/ninguem") is False


def test_mark_synced_preserves_history(linkedin_home):
    """The rewrite must not drop rows — it is the one non-append operation."""
    from tools.linkedin import ledger

    ledger.append("connect", "https://www.linkedin.com/in/a", "failed:x")
    ledger.append("connect", "https://www.linkedin.com/in/a", "ok", ledger.SYNC_PENDING)
    ledger.append("message", "https://www.linkedin.com/in/b", "ok", ledger.SYNC_PENDING)

    before = len(ledger.read_rows())
    ledger.mark_synced("connect", "https://www.linkedin.com/in/a")
    assert len(ledger.read_rows()) == before
    assert ledger.count_today("connect") == 1  # the failure still does not count


def test_mark_synced_clears_message_queue_without_narrowing(linkedin_home):
    """A DM sync must drain the message queue by default.

    The handler used to default `ledger_action` to "connect" — and the
    parameter was not even in the schema, so a caller had no way to override
    it. Syncing after a message silently no-opped and that row stayed pending
    forever, making `status` report phantom work every cycle.
    """
    from tools.linkedin_tool import _handle

    from tools.linkedin import ledger

    ledger.append("message", "https://www.linkedin.com/in/ana", "ok", ledger.SYNC_PENDING)

    result = json.loads(
        asyncio.run(
            _handle({"action": "mark_synced", "profiles": ["https://www.linkedin.com/in/ana"]})
        )
    )

    assert result["cleared"] == ["https://www.linkedin.com/in/ana"]
    assert result["still_pending"] == 0


def test_mark_synced_drains_every_queue_for_one_profile(linkedin_home):
    """Both rows must clear — evaluating with any() would short-circuit."""
    from tools.linkedin import ledger
    from tools.linkedin_tool import _handle

    profile = "https://www.linkedin.com/in/bruno"
    ledger.append("connect", profile, "ok", ledger.SYNC_PENDING)
    ledger.append("message", profile, "ok", ledger.SYNC_PENDING)

    result = json.loads(
        asyncio.run(_handle({"action": "mark_synced", "profiles": [profile]}))
    )

    assert result["still_pending"] == 0


def test_mark_synced_honours_explicit_narrowing(linkedin_home):
    from tools.linkedin import ledger
    from tools.linkedin_tool import _handle

    profile = "https://www.linkedin.com/in/carla"
    ledger.append("connect", profile, "ok", ledger.SYNC_PENDING)
    ledger.append("message", profile, "ok", ledger.SYNC_PENDING)

    result = json.loads(
        asyncio.run(
            _handle(
                {
                    "action": "mark_synced",
                    "profiles": [profile],
                    "ledger_action": "connect",
                }
            )
        )
    )

    remaining = ledger.pending_sync()
    assert result["still_pending"] == 1
    assert remaining[0]["action"] == "message"


def test_ledger_action_is_advertised_in_the_schema(linkedin_home):
    """An undocumented parameter is one the model can never set."""
    from tools.linkedin_tool import _SCHEMA

    prop = _SCHEMA["parameters"]["properties"].get("ledger_action")
    assert prop is not None, "ledger_action missing from the tool schema"
    assert set(prop["enum"]) == {"connect", "message", "check_accepted"}


def test_ledger_pending_sync_queue(linkedin_home):
    from tools.linkedin import ledger

    ledger.append("connect", "https://www.linkedin.com/in/a", "ok", ledger.SYNC_PENDING)
    ledger.append("connect", "https://www.linkedin.com/in/b", "ok", ledger.SYNC_SYNCED)

    pending = ledger.pending_sync()
    assert len(pending) == 1
    assert pending[0]["profile"] == "https://www.linkedin.com/in/a"


# ---------------------------------------------------------------------------
# Profile lock
# ---------------------------------------------------------------------------

def test_lock_blocks_concurrent_acquisition(linkedin_home):
    """Two Chromium processes on one profile corrupt the saved session."""
    from tools.linkedin.lock import ProfileBusy, profile_lock

    with profile_lock("outer"):
        with pytest.raises(ProfileBusy):
            with profile_lock("inner"):
                pass


def test_lock_is_released_on_exit(linkedin_home):
    from tools.linkedin.lock import profile_lock
    from tools.linkedin.paths import lock_path

    with profile_lock("a"):
        assert lock_path().exists()
    assert not lock_path().exists()


def test_lock_reclaims_stale_lock_from_dead_process(linkedin_home):
    """A crashed run must not freeze the funnel until someone deletes a file."""
    from tools.linkedin.lock import profile_lock, read_holder
    from tools.linkedin.paths import ensure_dirs, lock_path

    ensure_dirs()
    lock_path().write_text(json.dumps({"pid": 999_999, "acquired_at": "x"}))
    assert read_holder() is None

    with profile_lock("after-stale"):
        assert read_holder()["pid"] == os.getpid()


def test_lock_reclaims_corrupt_lockfile(linkedin_home):
    from tools.linkedin.lock import profile_lock
    from tools.linkedin.paths import ensure_dirs, lock_path

    ensure_dirs()
    lock_path().write_text("not json")
    with profile_lock("after-garbage"):
        pass


# ---------------------------------------------------------------------------
# Acceptance matching
# ---------------------------------------------------------------------------

def _row(url, has_button=True, name="Ana Silva"):
    return {"name": name, "name_norm": name.lower(), "profile_url": url, "has_message_button": has_button}


def test_match_connection_rows_matches_relative_href():
    """Cards carry relative hrefs; HubSpot stores absolute URLs."""
    from tools.linkedin.actions import match_connection_rows

    matched = match_connection_rows(
        ["https://www.linkedin.com/in/ana"], [_row("/in/ana")]
    )
    assert matched["https://www.linkedin.com/in/ana"]["match_reason"] == "profile_slug"


def test_match_connection_rows_reports_ambiguity():
    """Guessing here mislabels a stranger as a connection and costs an InMail."""
    from tools.linkedin.actions import match_connection_rows

    matched = match_connection_rows(
        ["https://www.linkedin.com/in/ana"], [_row("/in/ana"), _row("/in/ana")]
    )
    assert matched["https://www.linkedin.com/in/ana"]["ambiguous"] is True


def test_match_connection_rows_ignores_unrelated():
    from tools.linkedin.actions import match_connection_rows

    assert match_connection_rows(
        ["https://www.linkedin.com/in/ana"], [_row("/in/outro")]
    ) == {}


# ---------------------------------------------------------------------------
# Connect-button ownership
#
# Regression tests for a real defect found on the first live invitation
# (2026-08-07, profile /in/tiago-alvisi). A LinkedIn profile page embeds other
# people's Connect buttons — the "More profiles for you" sidebar and every post
# in the activity feed carry "Convidar <name> para se conectar" controls. The
# unscoped search matched a stranger's button, so a follow-only profile could
# have sent an invitation to the wrong person. The fixture is that page's real
# markup, reduced to the profile card plus three sidebar buttons.
# ---------------------------------------------------------------------------

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "linkedin_profile_follow_only.html")


@pytest.mark.parametrize(
    "owner,label,expected",
    [
        ("Tiago Alvisi", "Convidar Caroline Savioli para se conectar", False),
        ("Tiago Alvisi", "Convidar Tiago Alvisi para se conectar", True),
        ("Tiago Alvisi", "Invite Tiago Alvisi to connect", True),
        ("Tiago Alvisi", "Conectar", True),      # unlabelled: own action bar
        ("Tiago Alvisi", "", True),
        # Owner unknown and the label names someone: refuse. An invitation
        # cannot be taken back, so the ambiguous case must not click.
        ("", "Convidar Caroline Savioli para se conectar", False),
        ("", "Conectar", True),
    ],
)
def test_belongs_to_profile(owner, label, expected):
    from tools.linkedin.actions import _belongs_to_profile

    class _Control:
        async def get_attribute(self, _name):
            return label

    assert asyncio.run(_belongs_to_profile(_Control(), owner)) is expected


@pytest.mark.skipif(
    not os.path.exists(FIXTURE), reason="captured LinkedIn fixture not present"
)
def test_connect_button_not_taken_from_sidebar():
    """The real page that exposed the bug must now yield no button.

    Tiago's profile is follow-only — LinkedIn offers "Seguir", not "Conectar".
    The only Connect controls present belong to other people, so the correct
    outcome is None (reported as skipped), never a stranger's button.
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    from tools.linkedin.actions import _find_connect_button, _profile_owner

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f"file://{FIXTURE}", wait_until="domcontentloaded")
            owner = await _profile_owner(page)
            found = await _find_connect_button(page, owner)
            await browser.close()
            return owner, found

    owner, found = asyncio.run(run())
    assert owner.startswith("Tiago"), f"owner detection regressed: {owner!r}"
    assert found is None, "picked up a Connect button that belongs to someone else"


_SIDEBAR = (
    '<aside><button aria-label="Convidar Caroline Savioli para se conectar">'
    "Conectar</button></aside>"
)

_MENU_SCRIPT = (
    "<script>document.getElementById('more').onclick=()=>"
    "{document.getElementById('menu').style.display='block';}</script>"
)

# "Conectar" exposed directly on the profile card (Alexandra Fiuza layout).
PROFILE_CONNECT_EXPOSED = f"""<main><section>
<h1>Alexandra Fiuza</h1><span>Alexandra Fiuza · 2º</span>
<button aria-label="Convidar Alexandra Fiuza para se conectar">Conectar</button>
<button>Enviar mensagem</button><button aria-label="Mais">···</button>
</section></main>{_SIDEBAR}"""

# "Conectar" only inside the "···" overflow menu (Tiago Alvisi layout).
PROFILE_CONNECT_IN_MENU = f"""<main><section>
<h1>Tiago Alvisi</h1><span>Tiago Alvisi · 2º</span>
<button>Seguir</button><button>Enviar mensagem</button>
<button id="more" aria-label="Mais">···</button>
</section></main>{_SIDEBAR}
<div class="artdeco-dropdown__content" style="display:none" id="menu">
  <div role="button">Salvar como PDF</div>
  <div role="button">Conectar</div>
  <div role="button">Denunciar/bloquear</div>
</div>{_MENU_SCRIPT}"""

# Genuinely follow-only: the menu opens but offers no Connect at all.
PROFILE_FOLLOW_ONLY = f"""<main><section>
<h1>Alguem Follow Only</h1><span>· 2º</span>
<button>Seguir</button><button>Enviar mensagem</button>
<button id="more" aria-label="Mais">···</button>
</section></main>{_SIDEBAR}
<div class="artdeco-dropdown__content" style="display:none" id="menu">
  <div role="button">Salvar como PDF</div><div role="button">Denunciar/bloquear</div>
</div>{_MENU_SCRIPT}"""


@pytest.mark.parametrize(
    "html,expected",
    [
        (PROFILE_CONNECT_EXPOSED, "Convidar Alexandra Fiuza para se conectar"),
        (PROFILE_CONNECT_IN_MENU, "Conectar"),
        (PROFILE_FOLLOW_ONLY, None),
    ],
    ids=["connect-on-card", "connect-in-overflow-menu", "follow-only"],
)
def test_find_connect_button_across_profile_layouts(html, expected):
    """Connect lives in one of two places, and neither search may go global.

    LinkedIn either shows Connect on the profile card or hides it behind the
    "···" menu when it wants to promote Follow. Both sidebar and activity-feed
    cards carry other people's Connect buttons *earlier in DOM order*, so an
    unscoped query returns a stranger — the defect seen live on 2026-08-07.
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    from tools.linkedin.actions import _find_connect_button, _profile_owner

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            owner = await _profile_owner(page)
            button = await _find_connect_button(page, owner)
            label = None
            if button is not None:
                label = (await button.get_attribute("aria-label")) or (
                    await button.inner_text()
                ).strip()
            await browser.close()
            return label

    assert asyncio.run(run()) == expected


def test_overflow_menu_requires_confirmation():
    """A "···" click that opens nothing must not fall back to a global search.

    Without the marker check, a failed menu open is indistinguishable from a
    menu without Connect, and the caller would happily invite whoever the
    sidebar offers.
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    from tools.linkedin.actions import _open_overflow_menu, _profile_scope

    html = f"""<main><section>
    <h1>Alguem</h1><button id="more" aria-label="Mais">···</button>
    </section></main>{_SIDEBAR}
    <div class="artdeco-dropdown__content" style="display:none" id="menu">
      <div role="button">Conectar</div>
    </div>"""  # no script: the click opens nothing

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            menu = await _open_overflow_menu(page, await _profile_scope(page))
            await browser.close()
            return menu

    assert asyncio.run(run()) is None


@pytest.mark.skipif(
    not os.path.exists(FIXTURE), reason="captured LinkedIn fixture not present"
)
def test_message_button_alone_is_not_proof_of_connection():
    """A 2nd-degree profile with "Enviar mensagem" must not read as accepted.

    LinkedIn shows a Message control on creator / open-profile accounts that
    are still 2nd degree, and the composer it opens is a paid InMail. Without
    the degree-badge check this profile would flow accepted -> CONNECTED ->
    message, spending an InMail credit on someone who never connected.
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    import tools.linkedin.actions as actions

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f"file://{FIXTURE}", wait_until="domcontentloaded")

            # The fixture is already loaded; neutralize navigation and the
            # network-dependent block check.
            async def _noop(*_a, **_k):
                return None

            page.goto = _noop
            page.wait_for_timeout = lambda _ms: asyncio.sleep(0)
            with patch.object(actions, "raise_if_blocked", _noop):
                accepted = await actions._profile_shows_message_button(page, "x")
                opened, reason = await actions._open_thread_from_profile(page, "x")
            await browser.close()
            return accepted, opened, reason

    accepted, opened, reason = asyncio.run(run())
    assert accepted is False, "2nd-degree profile misreported as an accepted connection"
    assert opened is False
    assert reason == "blocked_non_first_degree"


# ---------------------------------------------------------------------------
# Connections scraping and degree reading
#
# Two regressions from 2026-08-07, both of which made an accepted invitation
# read as still pending:
#
# 1. LinkedIn wraps connection cards in `display: contents` elements. Those
#    generate no box, so width/height are 0 and the visibility test rejected
#    every card — the bulk scrape returned nothing and each profile fell
#    through to the slow per-profile path.
# 2. The degree was read by scanning the card's text for "2º"/"3º", which
#    also matched recommendations and "people also viewed" written by other
#    people. A 1st-degree contact with a 2nd-degree recommender looked like a
#    stranger.
# ---------------------------------------------------------------------------

def _eval_on(html, script, arg=None):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            result = (
                await page.evaluate(script, arg)
                if arg is not None
                else await page.evaluate(script)
            )
            await browser.close()
            return result

    return asyncio.run(run())


def test_connections_scrape_handles_display_contents():
    """Cards wrapped in `display: contents` must still be scraped."""
    from tools.linkedin.selectors import CONNECTIONS_SCRAPE_SCRIPT

    html = """<main>
      <div style="display: contents">
        <a href="/in/ana">Ana Silva</a>
        <span>Head de E-commerce</span>
        <button>Mensagem</button>
      </div>
    </main>"""

    rows = _eval_on(html, CONNECTIONS_SCRAPE_SCRIPT)
    assert len(rows) == 1, "display:contents card was treated as hidden"
    assert rows[0]["name"] == "Ana Silva"
    assert rows[0]["hasMessageButton"] is True


def test_connections_scrape_survives_obfuscated_classes():
    """No `li` and no `mn-connection-card` — only obfuscated divs."""
    from tools.linkedin.selectors import CONNECTIONS_SCRAPE_SCRIPT

    html = """<main><div class="_6ebd00b4">
      <div class="_72dac8af"><a href="/in/ana">Ana Silva</a>
        <span>CEO</span><button>Mensagem</button></div>
      <div class="_72dac8af"><a href="/in/bruno">Bruno Costa</a>
        <span>CTO</span><button>Mensagem</button></div>
    </div></main>"""

    rows = _eval_on(html, CONNECTIONS_SCRAPE_SCRIPT)
    names = sorted(r["name"] for r in rows)
    assert names == ["Ana Silva", "Bruno Costa"]


def test_connections_scrape_still_ignores_inmail_decoy():
    from tools.linkedin.selectors import CONNECTIONS_SCRAPE_SCRIPT

    html = """<main><div class="_x"><a href="/in/erica">Erica Melo</a>
      <button aria-label="Premium">Dê um alô</button></div></main>"""

    rows = _eval_on(html, CONNECTIONS_SCRAPE_SCRIPT)
    assert rows and rows[0]["hasMessageButton"] is False


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<main><h1>Tiago Alvisi</h1><span>· 1º</span></main>", "1"),
        ("<main><h1>Tiago Alvisi</h1><span>· 2º</span></main>", "2"),
        ("<main><h1>Someone</h1><span>· 3rd</span></main>", "3"),
        # The badge next to the name wins over other people's badges further
        # down the page — this is the exact shape that broke acceptance.
        (
            "<main><h1>Tiago Alvisi</h1><span>· 1º</span>"
            "<section><p>Recomendação</p><p>Felipe Ennes · 2º</p></section></main>",
            "1",
        ),
        ("<main><h1>Sem badge</h1></main>", ""),
    ],
    ids=["first", "second", "third-en", "recommendation-does-not-win", "no-badge"],
)
def test_profile_degree_reads_the_owner_badge(html, expected):
    from tools.linkedin.selectors import PROFILE_DEGREE_SCRIPT

    assert _eval_on(html, PROFILE_DEGREE_SCRIPT)["degree"] == expected


# ---------------------------------------------------------------------------
# Block detection
#
# Regression for a false positive on 2026-08-07: a feed post reading "ready to
# tackle challenges" matched the bare word "challenge" and aborted the whole
# run on a perfectly healthy session. Profile pages and the feed are full of
# other people's prose, so single common words cannot be block signals.
# ---------------------------------------------------------------------------

_APP_SHELL = "<a href='/feed/'>Feed</a><input placeholder='Pesquisar'/>"


@pytest.mark.parametrize(
    "url,body,expected",
    [
        # Real blocks: LinkedIn navigates to an interstitial.
        ("https://www.linkedin.com/checkpoint/challenge/", "<p>Verify</p>", True),
        ("https://www.linkedin.com/authwall", "<p>Sign in</p>", True),
        # Real block by copy, with no app shell present.
        (
            "https://www.linkedin.com/feed/",
            "<h1>Let's do a quick security check</h1>",
            True,
        ),
        # Ordinary feed content that merely contains the words.
        (
            "https://www.linkedin.com/feed/",
            f"{_APP_SHELL}<p>Consistency: ready to tackle challenges every day.</p>",
            False,
        ),
        (
            "https://www.linkedin.com/in/someone",
            f"{_APP_SHELL}<p>We removed restricted content from our platform.</p>",
            False,
        ),
        ("https://www.linkedin.com/feed/", _APP_SHELL, False),
    ],
    ids=[
        "checkpoint-url",
        "authwall-url",
        "security-check-copy",
        "feed-post-says-challenges",
        "post-says-restricted",
        "healthy-feed",
    ],
)
def test_detect_block_ignores_third_party_prose(url, body, expected):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    from tools.linkedin.browser import detect_block

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.fulfill(status=200, content_type="text/html", body=body)
                ),
            )
            await page.goto(url)
            result = await detect_block(page)
            await browser.close()
            return result

    assert asyncio.run(run()) is expected


def test_block_hints_exclude_bare_common_words():
    """Guard against reintroducing single-word hints.

    "challenge" and "restricted" appear in ordinary posts; matching them
    halts healthy runs.
    """
    from tools.linkedin.browser import BLOCK_TEXT_HINTS

    assert "challenge" not in BLOCK_TEXT_HINTS
    assert "restricted" not in BLOCK_TEXT_HINTS
    assert all(len(hint.split()) > 1 for hint in BLOCK_TEXT_HINTS)


# ---------------------------------------------------------------------------
# Send confirmation
#
# Regression for a false negative found on the first live DM (2026-08-07):
# the message was delivered but reported "unclear", because the script looked
# for `.msg-s-event-listitem__body` and LinkedIn now ships obfuscated class
# names. A permanent false negative makes the action useless — every send
# would need manual review.
# ---------------------------------------------------------------------------

_SENT_MSG = "Oi Jaevillen! Mensagem de teste do Hermes - validando a automacao."


@pytest.mark.parametrize(
    "html,expected",
    [
        # Delivered: present in the thread under an obfuscated class, and the
        # composer has been cleared.
        (
            f"<div class='_a1b2'><p>{_SENT_MSG}</p></div>"
            "<div role='textbox' contenteditable='true'></div>",
            True,
        ),
        # Still typing: the text exists only inside the composer. Reporting
        # this as sent is what causes duplicate messages.
        (f"<div role='textbox' contenteditable='true'>{_SENT_MSG}</div>", False),
        # Composer emptied with no visible thread — treated as sent.
        ("<div role='textbox' contenteditable='true'></div>", True),
        # No composer at all: nothing to conclude.
        ("<div>sem composer</div>", False),
    ],
    ids=["in-thread", "still-in-composer", "composer-emptied", "no-composer"],
)
def test_confirm_sent_script(html, expected):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    from tools.linkedin.selectors import CONFIRM_SENT_SCRIPT

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            result = await page.evaluate(
                CONFIRM_SENT_SCRIPT, {"fullText": _SENT_MSG, "firstLine": _SENT_MSG}
            )
            await browser.close()
            return result

    assert asyncio.run(run()) is expected


def test_confirm_sent_does_not_depend_on_linkedin_class_names():
    """Guard against reintroducing class-based matching.

    The `.msg-s-*` selectors these replaced are gone from the live site.
    """
    from tools.linkedin.selectors import CONFIRM_SENT_SCRIPT

    assert "msg-s-event-listitem" not in CONFIRM_SENT_SCRIPT
    assert "msg-s-message-list" not in CONFIRM_SENT_SCRIPT


# ---------------------------------------------------------------------------
# Human pacing
#
# The point of these is statistical shape, not exact values. A uniform draw
# over a wide range is still a bot signature; what matters is that delays are
# fractional, right-skewed, and never repeat exactly.
# ---------------------------------------------------------------------------

def test_human_gap_is_fractional_and_varied():
    """Whole-second delays are a machine tell — no human waits exactly 8.000s."""
    from tools.linkedin.humanize import human_gap

    samples = [human_gap() for _ in range(200)]
    # Continuous draws essentially never collide; a handful of clamped values
    # at the bounds is fine, wholesale repetition is not.
    assert len(set(samples)) > 150, "delays repeat far too often"
    # Whole seconds should be vanishingly rare, not merely uncommon.
    whole = sum(1 for s in samples if float(s).is_integer())
    assert whole <= 2, f"{whole} delays landed on exact seconds"


def test_human_gap_is_right_skewed():
    """Human inter-action gaps cluster low with a long tail, unlike uniform."""
    import statistics

    from tools.linkedin.humanize import human_gap

    samples = [human_gap() for _ in range(5000)]
    mean = statistics.mean(samples)
    median = statistics.median(samples)

    assert mean > median, "distribution is not right-skewed"
    # A uniform draw would put the median at the midpoint of the range; the
    # log-normal keeps it well below.
    assert median < (min(samples) + max(samples)) / 2


def test_human_gap_respects_bounds():
    from tools.linkedin.humanize import PROFILE_GAP_MAX, PROFILE_GAP_MIN, human_gap

    samples = [human_gap() for _ in range(2000)]
    assert min(samples) >= PROFILE_GAP_MIN
    assert max(samples) <= PROFILE_GAP_MAX


def test_jitter_varies_fixed_waits():
    """Identical navigation steps must not share an identical duration."""
    from tools.linkedin.humanize import jitter

    values = [jitter(2500) for _ in range(100)]
    assert len(set(values)) > 95
    assert all(v > 0 for v in values)
    assert 1200 < statistics_mean(values) < 3800


def statistics_mean(values):
    return sum(values) / len(values)


def test_jitter_never_returns_non_positive():
    from tools.linkedin.humanize import jitter

    assert all(jitter(1) > 0 for _ in range(200))
    assert jitter(0) > 0


def test_batch_ceiling_fits_dispatch_timeout():
    """The batch cap exists to stay under _run_async's 300s ceiling.

    Human pacing made the old cap of 5 overrun roughly 38% of the time; this
    guards against someone raising it back without redoing that arithmetic.
    """
    from tools.linkedin.humanize import PROFILE_GAP_MEDIAN
    from tools.linkedin_tool import MAX_PROFILES_PER_CALL

    per_profile_work = 20.0  # generous: load, read, click, confirm
    worst_case = MAX_PROFILES_PER_CALL * per_profile_work + (
        MAX_PROFILES_PER_CALL - 1
    ) * PROFILE_GAP_MEDIAN
    assert worst_case < 300, "batch cap no longer fits the dispatch timeout"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_load_template_strips_frontmatter(linkedin_home):
    from tools.linkedin import templates
    from tools.linkedin.paths import templates_dir

    templates_dir().mkdir(parents=True, exist_ok=True)
    (templates_dir() / "dm.md").write_text(
        "---\nname: dm\nstage: message_1\n---\nOi {first_name}!", encoding="utf-8"
    )

    assert templates.load_template("dm") == "Oi {first_name}!"
    assert templates.load_template("missing") is None


def test_load_template_rejects_path_traversal(linkedin_home):
    """A crafted template name must not read outside the templates folder."""
    from tools.linkedin import templates

    assert templates.load_template("../../../etc/passwd") is None


def test_list_templates_reports_metadata(linkedin_home):
    from tools.linkedin import templates
    from tools.linkedin.paths import templates_dir

    templates_dir().mkdir(parents=True, exist_ok=True)
    (templates_dir() / "a.md").write_text("---\nstage: message_1\n---\nBody", encoding="utf-8")

    listed = templates.list_templates()
    assert listed[0]["name"] == "a"
    assert listed[0]["stage"] == "message_1"


# ---------------------------------------------------------------------------
# Registration and availability gating
# ---------------------------------------------------------------------------

def test_tool_imports_without_playwright():
    """The module is imported in every Hermes session by discover_builtin_tools.

    A top-level `import playwright` would break startup for every user who
    has never installed the browser extra.
    """
    with patch.dict("sys.modules", {"playwright": None}):
        module = importlib.import_module("tools.linkedin_tool")
        importlib.reload(module)
    assert module is not None


def test_tool_is_registered():
    import tools.linkedin_tool  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("linkedin")
    assert entry is not None
    assert entry.toolset == "linkedin"
    assert entry.is_async is True


def test_toolset_is_declared():
    from toolsets import TOOLSETS

    assert TOOLSETS["linkedin"]["tools"] == ["linkedin"]


def test_check_fn_gates_on_playwright():
    from tools.linkedin_tool import check_linkedin_requirements

    with patch("tools.linkedin.browser.playwright_available", return_value=False):
        assert check_linkedin_requirements() is False
    with patch("tools.linkedin.browser.playwright_available", return_value=True):
        assert check_linkedin_requirements() is True


# ---------------------------------------------------------------------------
# Handler argument validation
# ---------------------------------------------------------------------------

def _call(args):
    from tools.linkedin_tool import _handle

    return json.loads(asyncio.run(_handle(args)))


def test_handler_rejects_oversized_batch(linkedin_home):
    """The 300s dispatch ceiling is what caps the batch at five."""
    from tools.linkedin_tool import MAX_PROFILES_PER_CALL

    profiles = [f"https://www.linkedin.com/in/p{i}" for i in range(MAX_PROFILES_PER_CALL + 1)]
    result = _call({"action": "connect", "profiles": profiles})

    assert result["status"] == "error"
    assert "at most" in result["message"]


def test_handler_rejects_non_profile_url(linkedin_home):
    result = _call({"action": "connect", "profiles": ["https://example.com/x"]})
    assert result["status"] == "error"


def test_handler_rejects_empty_profiles(linkedin_home):
    result = _call({"action": "connect", "profiles": []})
    assert result["status"] == "error"


def test_handler_requires_message_body(linkedin_home):
    result = _call({"action": "message", "profiles": ["https://www.linkedin.com/in/a"]})
    assert result["status"] == "error"
    assert "template" in result["message"]


def test_handler_rejects_unknown_action(linkedin_home):
    assert _call({"action": "nope"})["status"] == "error"


def test_handler_status_reports_quota(linkedin_home):
    result = _call({"action": "status"})

    assert result["status"] == "ok"
    assert result["remaining_today"]["connect"] == 5
    assert result["remaining_today"]["message"] == 5


def test_handler_deduplicates_profiles(linkedin_home):
    """Duplicates in one batch must not consume two quota slots."""
    from tools.linkedin_tool import _normalize_profiles

    profiles, err = _normalize_profiles(
        ["https://www.linkedin.com/in/ana", "linkedin.com/in/ana/"]
    )
    assert err is None
    assert profiles == ["https://www.linkedin.com/in/ana"]
