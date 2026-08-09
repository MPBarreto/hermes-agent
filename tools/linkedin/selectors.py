"""Selectors and injected DOM scripts for LinkedIn.

Ported verbatim in spirit from linkedin-lead-bot, which learned these
against the live site. Two conventions run through everything here and are
load-bearing:

* **Bilingual.** The account locale flips between PT-BR and EN — sometimes
  mid-session — so every text match lists both.
* **Premium/InMail exclusion.** LinkedIn renders a "Message" control on
  profiles you are *not* connected to, which opens a paid InMail composer.
  Treating it as evidence of a connection both misreports acceptance and
  burns InMail credits, so every scan skips controls whose text or aria
  label mentions InMail, Premium, or "dê um alô".

Selector lists are ordered most-specific first; the caller takes the first
visible match.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Connection invitations
# ---------------------------------------------------------------------------

CONNECT_BUTTON_SELECTORS = [
    "button:has-text('Conectar')",
    "button:has-text('Connect')",
    "a:has-text('Conectar')",
    "a:has-text('Connect')",
    "a[aria-label*='Convidar'][href*='custom-invite']",
    "a[aria-label*='Invite'][href*='custom-invite']",
    "a[aria-label*='Convidar'][href*='search-custom-invite']",
    "a[aria-label*='Invite'][href*='search-custom-invite']",
    "[role='button']:has-text('Conectar')",
    "[role='button']:has-text('Connect')",
]

# On many profiles "Connect" is hidden behind the overflow menu.
MORE_BUTTON_SELECTORS = [
    "button[aria-label*='Mais']",
    "button[aria-label*='More']",
    "button:has-text('Mais')",
    "button:has-text('More')",
]

# The profile's own action bar. A LinkedIn profile page embeds *other*
# people's Connect buttons — the "More profiles for you" sidebar and every
# post in the activity feed carry their own "Convidar X para se conectar"
# controls. Searching the whole document finds those and would invite the
# wrong person, so the connect flow is scoped to the top card only.
PROFILE_ACTION_SCOPES = [
    "main .ph5",                       # profile header block
    "main section:first-of-type",      # top card fallback
    "main",                            # last resort, still excludes the sidebar
]

# The overflow ("···") dropdown that holds Connect on profiles where LinkedIn
# promotes Follow/Message instead. It renders in a portal outside the profile
# card, so the post-click search must be scoped to the menu itself — the
# sidebar's Connect buttons otherwise win on DOM order.
OVERFLOW_MENU_SCOPES = [
    ".artdeco-dropdown__content--is-open",
    ".artdeco-dropdown__content",
    "div[role='menu']",
    "ul[role='menu']",
]

# Read the connection degree from the profile's own badge.
#
# Scanning the card's text for "2º"/"3º" is not safe: recommendations,
# "people also viewed", and highlight sections all render other people's
# degree badges inside the profile page, and any of them makes a 1st-degree
# contact look like a stranger (observed 2026-08-07, where a recommendation
# from a 2nd-degree contact made an accepted connection read as pending).
#
# The badge sits next to the name in the top card, so anchor on the name and
# take the first degree marker that follows it in document order.
PROFILE_DEGREE_SCRIPT = """() => {
    const main = document.querySelector('main') || document.body;
    const text = (main.innerText || '');
    const name = (() => {
        const h1 = main.querySelector('h1');
        if (h1 && h1.innerText.trim()) return h1.innerText.trim();
        const section = main.querySelector('section');
        if (!section) return '';
        const first = (section.innerText || '').split('\\n')
            .map((l) => l.trim()).filter(Boolean)[0];
        return first || '';
    })();
    if (!name) return { degree: '', name: '' };

    const start = text.indexOf(name);
    if (start < 0) return { degree: '', name };
    // Look only at the sliver right after the name — the badge is adjacent.
    const window_ = text.slice(start, start + 160);

    // Two ordinals can sit side by side in that window. Comparing the same
    // profile before and after it accepted an invitation showed:
    //   2nd degree -> "· 1º" AND "• 2º"
    //   1st degree -> "· 1º" only
    // So "· 1º" is not the connection degree (it is a separate marker that
    // never changes); the bullet-prefixed one is. Prefer "•" and fall back to
    // "·" only when no bullet badge exists.
    const bullet = /•\\s*([123])(?:º|st|nd|rd)/.exec(window_);
    if (bullet) return { degree: bullet[1], name };

    const middot = /·\\s*([123])(?:º|st|nd|rd)/.exec(window_);
    if (middot) return { degree: middot[1], name };

    const bare = /(?:^|\\s)([123])(?:º|st|nd|rd)(?:\\s|$)/.exec(window_);
    return { degree: bare ? bare[1] : '', name };
}"""

# Items that only exist inside the profile overflow menu — used to confirm
# the menu actually opened before trusting anything found in it.
OVERFLOW_MENU_MARKERS = (
    "Salvar como PDF",
    "Save to PDF",
    "Denunciar/bloquear",
    "Report/Block",
    "Sobre este usuário",
    "About this profile",
)

# Invitations are always sent without a note: free accounts get a small
# monthly quota of notes, and spending it does not measurably improve
# acceptance rates.
SEND_WITHOUT_NOTE_SELECTORS = [
    "button:has-text('Enviar sem nota')",
    "button:has-text('Send without a note')",
    "button:has-text('Enviar agora')",
    "button:has-text('Send now')",
]

# Text that confirms an invitation is now pending.
PENDING_HINTS = ("Pendente", "Pending", "Convite enviado", "Invitation sent")

# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

MESSAGE_BOX_SELECTORS = [
    "div[role='textbox'][contenteditable='true']",
    "div.msg-form__contenteditable[contenteditable='true']",
    "[contenteditable='true']",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "button:has-text('Enviar')",
    "button:has-text('Send')",
    "button[aria-label*='Enviar']",
    "button[aria-label*='Send']",
]

CONNECTIONS_SEARCH_SELECTORS = [
    "input[placeholder*='Pesquisar']",
    "input[placeholder*='Search']",
    "input[aria-label*='Pesquisar']",
    "input[aria-label*='Search']",
    "input[role='combobox']",
]

# ---------------------------------------------------------------------------
# Injected scripts
# ---------------------------------------------------------------------------

# Shared preamble: whitespace normalization, a real visibility test, and the
# Premium/InMail predicate.
_JS_HELPERS = """
    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
    const lower = (value) => normalize(value).toLowerCase();
    const isVisible = (element) => {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        // `display: contents` elements generate no box of their own — their
        // children render in their place — so width/height are always 0 even
        // though the content is plainly on screen. LinkedIn wraps connection
        // cards in exactly such elements, which made every card look hidden
        // and returned an empty connections list (observed 2026-08-07).
        if (style.display === 'contents') {
            return Array.from(element.children).some(isVisible)
                || Boolean((element.innerText || '').trim());
        }
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const isPremiumOrInmail = (text, aria) =>
        text.includes('dê um alô') || text.includes('inmail')
        || aria.includes('premium') || aria.includes('inmail');
    const isMessageControl = (element) => {
        const text = lower(element.innerText || element.textContent);
        const aria = lower(element.getAttribute('aria-label'));
        if (isPremiumOrInmail(text, aria)) return false;
        const byText = text === 'mensagem' || text === 'message'
            || text.includes('enviar mensagem') || text.includes('send message')
            || text.includes('send a message');
        const byAria = aria.includes('enviar mensagem para')
            || aria.includes('send a message to') || aria.includes('message to');
        return byText || byAria;
    };
    // Connection cards are structural, not semantic: LinkedIn dropped
    // `li` / `.mn-connection-card` in favour of obfuscated divs, so the old
    // selectors matched nothing and every acceptance check fell through to
    // the slow per-profile path (observed 2026-08-07).
    //
    // A card is the smallest block that owns exactly one profile link and
    // stays compact. Walk up from each /in/ link and keep the last ancestor
    // that still holds a single profile.
    const cardNodes = () => {
        const root = document.querySelector('main') || document.body;
        const legacy = Array.from(root.querySelectorAll(
            'li, .mn-connection-card, [data-view-name*="connections"]'));
        if (legacy.length) return legacy;

        const slugOf = (el) => {
            const a = el.querySelector ? el.querySelector('a[href*="/in/"]') : null;
            const m = a ? /\\/in\\/([^\\/?#]+)/.exec(a.getAttribute('href') || '') : null;
            return m ? m[1].toLowerCase() : '';
        };

        const seen = new Set();
        const cards = [];
        for (const link of Array.from(root.querySelectorAll('a[href*="/in/"]'))) {
            const wanted = /\\/in\\/([^\\/?#]+)/.exec(link.getAttribute('href') || '');
            if (!wanted) continue;
            const slug = wanted[1].toLowerCase();
            if (seen.has(slug)) continue;

            // Grow only while the block still describes this one person.
            // Allowing two profile links merges sibling cards into their
            // shared parent, and every card after the first is then dropped
            // as a duplicate.
            let node = link;
            let best = null;
            for (let i = 0; i < 6 && node; i += 1) {
                node = node.parentElement;
                if (!node || node === root) break;
                if ((node.innerText || '').length > 400) break;
                const links = node.querySelectorAll('a[href*="/in/"]');
                const distinct = new Set(Array.from(links).map((a) => {
                    const m = /\\/in\\/([^\\/?#]+)/.exec(a.getAttribute('href') || '');
                    return m ? m[1].toLowerCase() : '';
                }).filter(Boolean));
                if (distinct.size > 1) break;
                best = node;
            }
            if (best) { seen.add(slug); cards.push(best); }
        }
        return cards;
    };
    const readCard = (card) => {
        const profileLink = card.querySelector('a[href*="/in/"], a[href*="linkedin.com/in/"]');
        const rawHref = profileLink ? profileLink.getAttribute('href') || '' : '';
        const nameNode = card.querySelector(
            '.mn-connection-card__name, .artdeco-entity-lockup__title, [data-anonymize="person-name"]');
        // Fall back to the card's own text lines when the semantic classes
        // are absent: line 1 is the name, line 2 the headline. LinkedIn's
        // obfuscated markup leaves no other handle.
        // Split before normalizing — normalize() collapses the newlines.
        const lines = (card.innerText || card.textContent || '')
            .split('\\n').map((l) => l.trim()).filter(Boolean);
        const linkText = normalize(profileLink && (profileLink.innerText || profileLink.textContent));
        const name = normalize((nameNode && (nameNode.innerText || nameNode.textContent))
            || linkText || lines[0] || '');
        const headlineNode = card.querySelector(
            '.mn-connection-card__occupation, .artdeco-entity-lockup__subtitle, [data-anonymize="headline"]');
        const headline = normalize((headlineNode && (headlineNode.innerText || headlineNode.textContent))
            || (lines[1] || ''));
        let messageControl = null;
        for (const action of Array.from(card.querySelectorAll('button, a, [role="button"]'))) {
            if (!isVisible(action)) continue;
            if (isMessageControl(action)) { messageControl = action; break; }
        }
        return { name, headline, rawHref, messageControl,
                 cardText: normalize(card.innerText || card.textContent) };
    };
"""

# Is there a genuine (non-InMail) Message control on this profile? A 1st-degree
# connection has one; a pending invitation does not.
MESSAGE_EVIDENCE_SCRIPT = (
    """() => {"""
    + _JS_HELPERS
    + """
    const controls = Array.from(document.querySelectorAll('main button, main a, main [role="button"]'));
    for (const element of controls) {
        if (!isVisible(element)) continue;
        if (!isMessageControl(element)) continue;
        return {
            found: true,
            text: normalize(element.innerText || element.textContent),
            aria: normalize(element.getAttribute('aria-label')),
        };
    }
    return { found: false };
}"""
)

# Bulk-scrape the connections list. One page load answers "who accepted?" for
# the whole pending batch, instead of one profile visit per lead.
CONNECTIONS_SCRAPE_SCRIPT = (
    """() => {"""
    + _JS_HELPERS
    + """
    const rows = [];
    for (const card of cardNodes()) {
        if (!isVisible(card)) continue;
        if (!normalize(card.innerText || card.textContent)) continue;
        const info = readCard(card);
        if (!info.name && !info.rawHref) continue;
        rows.push({
            name: info.name,
            headline: info.headline,
            rawHref: info.rawHref,
            hasMessageButton: Boolean(info.messageControl),
        });
    }
    return rows;
}"""
)

# Enumerate connection cards, optionally filtered to one normalized name.
CONNECTIONS_CARD_SCRIPT = (
    """(targetNameNormalized) => {"""
    + _JS_HELPERS
    + """
    const target = lower(targetNameNormalized || '');
    const out = [];
    for (const card of cardNodes()) {
        if (!isVisible(card)) continue;
        const info = readCard(card);
        if (!info.name && !info.rawHref) continue;
        if (target && lower(info.name) !== target) continue;
        out.push({
            name: info.name,
            headline: info.headline,
            rawHref: info.rawHref,
            hasMessageButton: Boolean(info.messageControl),
            cardText: info.cardText,
        });
    }
    return out;
}"""
)

# Click the Message control on the card matching url-or-name. Matching and
# clicking happen in one pass so the row cannot shift between them.
CONNECTIONS_CLICK_MESSAGE_SCRIPT = (
    """({ profileUrl, name }) => {"""
    + _JS_HELPERS
    + """
    // Compare the /in/<slug> identifier: cards carry relative hrefs while
    // the caller passes an absolute URL, so full-string equality never hits.
    const slugOf = (value) => {
        const match = /\\/in\\/([^\\/?#]+)/.exec(normalize(value) || '');
        return match ? match[1].toLowerCase() : '';
    };
    const wantedSlug = slugOf(profileUrl);
    const wantedName = lower(name || '');
    for (const card of cardNodes()) {
        if (!isVisible(card)) continue;
        const info = readCard(card);
        if (!info.messageControl) continue;
        const urlHit = wantedSlug && slugOf(info.rawHref) === wantedSlug;
        const nameHit = wantedName && lower(info.name) === wantedName;
        if (!urlHit && !nameHit) continue;
        info.messageControl.scrollIntoView({ block: 'center', inline: 'center' });
        info.messageControl.click();
        return { clicked: true, name: info.name, href: info.rawHref };
    }
    return { clicked: false };
}"""
)

# Profile-page fallback for opening a conversation. Only ever used after the
# caller has established the contact is 1st-degree.
MESSAGE_BUTTON_SCRIPT = (
    """() => {"""
    + _JS_HELPERS
    + """
    const controls = Array.from(document.querySelectorAll('main button, main a, main [role="button"]'));
    for (const element of controls) {
        if (!isVisible(element)) continue;
        if (!isMessageControl(element)) continue;
        element.scrollIntoView({ block: 'center', inline: 'center' });
        element.click();
        return { clicked: true, text: normalize(element.innerText || element.textContent) };
    }
    return { clicked: false };
}"""
)

# The conversation overlay opens minimized to a footer pill on some paths.
# While collapsed the composer is not in the DOM at all, so it has to be
# expanded before anything can be typed or confirmed.
CONVERSATION_PILL_SELECTORS = [
    "div[class*='msg-overlay-bubble-header']",
    "div[class*='msg-convo-wrapper'] header",
    "button[aria-label*='Abrir conversa']",
    "button[aria-label*='Open conversation']",
    "div[data-testid*='conversation'] header",
]

# Did the message actually land? Either it appears in the thread, or the
# compose box emptied — both mean sent. Anything else is unconfirmed.
#
# Text-based, not class-based: LinkedIn ships obfuscated class names, so the
# old `.msg-s-event-listitem__body` selectors matched nothing and every send
# came back "unclear" even when it had gone through (observed 2026-08-07).
CONFIRM_SENT_SCRIPT = """({ fullText, firstLine }) => {
    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const full = normalize(fullText);
    const first = normalize(firstLine);
    const probe = (full || first).slice(0, 60);
    if (!probe) return false;

    const composer = document.querySelector(
        "div[role='textbox'][contenteditable='true'], textarea");
    const composerText = composer
        ? normalize(('value' in composer ? composer.value : composer.textContent) || '')
        : '';

    // The message is on screen somewhere that is not the composer.
    const inThread = Array.from(document.querySelectorAll('p, span, li, div'))
        .some((node) => {
            if (composer && (node === composer || composer.contains(node)
                             || node.contains(composer))) return false;
            if (node.children.length > 3) return false;  // prefer leaf-ish nodes
            return normalize(node.innerText || node.textContent || '').includes(probe);
        });
    if (inThread) return true;

    // Fallback: the composer emptied out, which only happens on send.
    return Boolean(composer) && composerText.length === 0;
}"""

# Fill the compose box, falling back to a synthetic InputEvent when Playwright's
# fill() is rejected by the rich-text editor.
INSERT_TEXT_SCRIPT = """(node, value) => {
    node.focus();
    if ('value' in node) {
        node.value = value;
        node.dispatchEvent(new Event('input', { bubbles: true }));
        node.dispatchEvent(new Event('change', { bubbles: true }));
        return;
    }
    node.textContent = value;
    node.dispatchEvent(new InputEvent('input', {
        bubbles: true, inputType: 'insertText', data: value }));
}"""

# Three-tier click: normal, scrolled, then raw DOM click. LinkedIn's sticky
# headers intercept pointer events often enough that the escalation matters.
FORCE_CLICK_SCRIPT = """(element) => {
    element.scrollIntoView({ block: 'center', inline: 'center' });
    element.click();
}"""
