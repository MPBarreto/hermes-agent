"""Filesystem layout for the LinkedIn tool.

Everything lives under ``$HERMES_HOME/linkedin``. Kept in its own module so
the pure-Python pieces (ledger, templates, utils) can locate their files
without importing anything that pulls in Playwright.
"""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_hermes_home

# The timezone the daily quota is measured in. The SDR crons run at 07:00 and
# 09:00 -03:00, so a UTC day boundary would cut the working window in half and
# hand out a second batch of invitations at 21:00 local time.
LOCAL_TZ_NAME = "America/Sao_Paulo"


def linkedin_home() -> Path:
    """Base directory for all LinkedIn tool state."""
    return get_hermes_home() / "linkedin"


def profile_dir() -> Path:
    """Persistent Chrome profile — this is what keeps the session logged in."""
    return linkedin_home() / "profile"


def templates_dir() -> Path:
    """User-editable message templates (one ``.md`` per template)."""
    return linkedin_home() / "templates"


def artifacts_dir() -> Path:
    """Screenshots and HTML snapshots captured when a step fails."""
    return linkedin_home() / "artifacts"


def lock_path() -> Path:
    """Lockfile guarding single-process access to the Chrome profile."""
    return linkedin_home() / "profile.lock"


def ledger_path(month: str) -> Path:
    """Ledger file for a given ``YYYY-MM``. Rotated monthly so the daily
    scan stays cheap as history accumulates."""
    return linkedin_home() / f"ledger-{month}.md"


def ensure_dirs() -> None:
    """Create the directory tree. Safe to call repeatedly."""
    for path in (linkedin_home(), profile_dir(), templates_dir(), artifacts_dir()):
        path.mkdir(parents=True, exist_ok=True)
