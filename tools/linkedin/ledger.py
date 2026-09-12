"""Append-only action log: daily quota, idempotency, HubSpot sync queue.

HubSpot is the source of truth for lead state. This file is the local
fallback that lets the tool answer three questions without a round trip to
an MCP server that may be down:

1. How many invitations/messages have I already sent today? (quota)
   Connection invitations also have independent 5-per-window caps: morning
   07:00–10:00 and evening 19:00–20:00 in America/Sao_Paulo.
2. Have I already acted on this profile today? (idempotency)
3. Which actions still need writing back to HubSpot? (reconciliation)

Markdown table rows, one per action, appended as they happen — never
rewritten. The bot this was ported from rewrote its entire JSON store on
every mutation, which is both O(n) per lead and a corruption window if the
process dies mid-write.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from tools.linkedin.paths import LOCAL_TZ_NAME, ensure_dirs, ledger_path
from tools.linkedin.utils import normalize_linkedin_url, utc_now_iso

# Actions that consume a daily quota. `check_accepted` is a read, so it is
# recorded for the audit trail but never counted against a limit.
QUOTA_ACTIONS = ("connect", "message")

# Connection invitations have one daily cap and two independent operational
# windows. The windows are deliberately narrow: the morning cron runs at 08h
# and the evening cron runs at 19h, both in the ledger's local timezone.
DAILY_LIMITS = {"connect": 10, "message": 5}
CONNECT_WINDOW_LIMITS = {"morning": 5, "evening": 5}
CONNECT_WINDOWS = {
    "morning": (time(7, 0), time(10, 0)),
    "evening": (time(19, 0), time(20, 0)),
}

SYNC_PENDING = "hubspot:pending"
SYNC_SYNCED = "hubspot:synced"
SYNC_NA = "hubspot:n/a"

_HEADER = (
    "# LinkedIn action ledger\n"
    "\n"
    "Append-only. One row per action. `when` is UTC; the daily quota is\n"
    f"measured in {LOCAL_TZ_NAME}.\n"
    "\n"
    "| when | action | profile | result | sync |\n"
    "|---|---|---|---|---|\n"
)

_ROW_RE = re.compile(
    r"^\|\s*(?P<when>[^|]+?)\s*\|\s*(?P<action>[^|]+?)\s*\|"
    r"\s*(?P<profile>[^|]+?)\s*\|\s*(?P<result>[^|]+?)\s*\|"
    r"\s*(?P<sync>[^|]*?)\s*\|\s*$"
)


def local_now() -> datetime:
    """Current time in the operating timezone."""
    return datetime.now(ZoneInfo(LOCAL_TZ_NAME))


def local_today() -> str:
    """Today's date in the operating timezone, as ``YYYY-MM-DD``."""
    return local_now().strftime("%Y-%m-%d")


def connect_window_for_local(value: datetime) -> Optional[str]:
    """Return the connection window containing a local datetime, if any."""
    local_value = value.astimezone(ZoneInfo(LOCAL_TZ_NAME)) if value.tzinfo else value
    current = local_value.time().replace(tzinfo=None)
    for name, (start, end) in CONNECT_WINDOWS.items():
        if start <= current < end:
            return name
    return None


def connect_window_for_timestamp(when_utc: str) -> Optional[str]:
    """Map a stored UTC timestamp to the local connection window."""
    try:
        parsed = datetime.fromisoformat(when_utc.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return None
    return connect_window_for_local(parsed.astimezone(ZoneInfo(LOCAL_TZ_NAME)))


def current_connect_window() -> Optional[str]:
    """Return the currently open connection window, if outbound is allowed."""
    return connect_window_for_local(local_now())


def _local_date_of(when_utc: str) -> str:
    """Convert a stored UTC timestamp to its local calendar date."""
    try:
        return (
            datetime.fromisoformat(when_utc.replace("Z", "+00:00"))
            .astimezone(ZoneInfo(LOCAL_TZ_NAME))
            .strftime("%Y-%m-%d")
        )
    except ValueError:
        return ""


def _month_of(date_str: str) -> str:
    return date_str[:7]


def append(action: str, profile: str, result: str, sync: str = SYNC_NA) -> None:
    """Record one completed action.

    Called immediately after each profile is handled, never batched at the
    end of a run — that is what makes a dispatch timeout or a crash
    recoverable instead of leaving the ledger disagreeing with LinkedIn.
    """
    ensure_dirs()
    path = ledger_path(_month_of(local_today()))
    if not path.exists():
        path.write_text(_HEADER, encoding="utf-8")

    profile = normalize_linkedin_url(profile) or profile
    # Guard the column separator so a stray pipe can't forge a row.
    cells = [c.replace("|", "\\|").replace("\n", " ") for c in (utc_now_iso(), action, profile, result, sync)]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("| " + " | ".join(cells) + " |\n")


def read_rows(date: Optional[str] = None) -> list[dict]:
    """Parse ledger rows, optionally filtered to one local date.

    Reads only the month file containing *date*, so the scan cost stays flat
    as history accumulates.
    """
    target = date or local_today()
    path = ledger_path(_month_of(target))
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    rows: list[dict] = []
    for line in text.splitlines():
        match = _ROW_RE.match(line)
        if not match:
            continue
        row = match.groupdict()
        if row["when"] == "when" or set(row["when"]) <= {"-", " "}:
            continue  # header or separator
        row["date"] = _local_date_of(row["when"])
        if row["date"] == target:
            rows.append(row)
    return rows


def count_today(action: str) -> int:
    """Successful actions of this type performed today, in local time."""
    return sum(
        1
        for row in read_rows()
        if row["action"] == action and row["result"] == "ok"
    )


def count_connect_window(window: str) -> int:
    """Successful connection invitations in one local window today."""
    if window not in CONNECT_WINDOW_LIMITS:
        raise ValueError(f"Unknown connection window: {window!r}")
    return sum(
        1
        for row in read_rows()
        if row["action"] == "connect"
        and row["result"] == "ok"
        and connect_window_for_timestamp(row["when"]) == window
    )


def remaining_connect_window(window: str) -> int:
    """Headroom left in one of today's connection windows."""
    limit = CONNECT_WINDOW_LIMITS.get(window)
    if limit is None:
        raise ValueError(f"Unknown connection window: {window!r}")
    return max(0, limit - count_connect_window(window))


def remaining_connect_windows_today() -> dict[str, int]:
    """Return independent headroom for the morning and evening windows."""
    return {window: remaining_connect_window(window) for window in CONNECT_WINDOW_LIMITS}


def remaining_today(action: str) -> int:
    """Headroom left under the daily cap. Unlimited actions report a large number."""
    limit = DAILY_LIMITS.get(action)
    if limit is None:
        return 1_000_000
    return max(0, limit - count_today(action))


def already_done_today(action: str, profile: str) -> bool:
    """True when this profile already received this action today.

    Prevents a re-run after a partial failure from sending a second
    invitation to someone who already got one.
    """
    target = normalize_linkedin_url(profile) or profile
    return any(
        row["action"] == action and row["profile"] == target and row["result"] == "ok"
        for row in read_rows()
    )


def pending_sync(dates: Optional[Iterable[str]] = None) -> list[dict]:
    """Rows still awaiting a HubSpot write-back.

    The agent drains this at the start of the next run; there is no retry
    daemon, by design.
    """
    out: list[dict] = []
    for date in dates or [local_today()]:
        out.extend(row for row in read_rows(date) if row["sync"] == SYNC_PENDING)
    return out


def mark_synced(
    action: str, profile: str, date: Optional[str] = None
) -> bool:
    """Flip a row from ``hubspot:pending`` to ``hubspot:synced``.

    Called after the HubSpot write succeeds. Without it the queue never
    drains: ``status`` keeps reporting the same rows and the next cycle
    rewrites contacts that are already correct.

    This is the one place the ledger is not append-only. It rewrites the
    month file through a temp file + ``os.replace`` so an interrupted write
    cannot truncate the history.
    """
    target_date = date or local_today()
    path = ledger_path(_month_of(target_date))
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (FileNotFoundError, OSError):
        return False

    wanted = normalize_linkedin_url(profile) or profile
    changed = False
    out: list[str] = []
    for line in lines:
        match = _ROW_RE.match(line.rstrip("\n"))
        if (
            not changed
            and match
            and match.group("action") == action
            and match.group("profile") == wanted
            and match.group("sync") == SYNC_PENDING
            and _local_date_of(match.group("when")) == target_date
        ):
            out.append(line.replace(SYNC_PENDING, SYNC_SYNCED))
            changed = True
        else:
            out.append(line)

    if not changed:
        return False

    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.replace(tmp, path)
    return True
