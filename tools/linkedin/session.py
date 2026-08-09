"""Interactive login, run as a detached process.

Signing in means typing a password and clearing 2FA — minutes of human time,
against a 300s dispatch budget. So ``login`` does not wait: it spawns a
detached Python process that owns the browser window and the profile lock,
and returns immediately. The user signs in, closes the window, and confirms
with ``login_status``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tools.linkedin.lock import read_holder
from tools.linkedin.paths import linkedin_home

# Runs in the detached child. Holds the lock for the whole browser lifetime
# so no automated action can open the profile mid-login, and exits as soon as
# the user closes the window.
_LOGIN_SCRIPT = """
import asyncio, sys

async def main():
    from tools.linkedin.browser import LOGIN_URL, ensure_playwright
    from tools.linkedin.lock import profile_lock
    from tools.linkedin.paths import ensure_dirs, profile_dir

    ensure_playwright()
    ensure_dirs()
    from playwright.async_api import async_playwright

    with profile_lock("login"):
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir()),
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                ignore_https_errors=True,
            )
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            # Block until the human closes the window; the session is
            # persisted into the profile directory on the way out.
            await context.wait_for_event("close", timeout=0)

asyncio.run(main())
"""


def login_log_path() -> Path:
    return linkedin_home() / "login.log"


def spawn_login() -> dict:
    """Launch the login browser and return without waiting."""
    holder = read_holder()
    if holder is not None:
        return {
            "status": "busy",
            "message": (
                f"The LinkedIn profile is already in use by PID {holder.get('pid')} "
                f"(action={holder.get('action')!r}). Close that window or wait for "
                "the run to finish."
            ),
        }

    linkedin_home().mkdir(parents=True, exist_ok=True)
    log_file = login_log_path()
    repo_root = Path(__file__).resolve().parents[2]

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    with log_file.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "-c", _LOGIN_SCRIPT],
            cwd=str(repo_root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives this tool call
        )

    return {
        "status": "awaiting_login",
        "pid": process.pid,
        "message": (
            "A Chromium window is opening on the LinkedIn login page. Sign in, "
            "clear 2FA if prompted, wait for the feed to load, then close the "
            "window to save the session. Confirm with action='login_status'."
        ),
        "log": str(log_file),
    }
