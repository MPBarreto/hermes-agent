"""Single-process guard for the persistent Chrome profile.

Chromium refuses to open a ``user_data_dir`` that another process already
holds, and forcing it corrupts the profile — which here means losing the
LinkedIn session the user logged in by hand. Two concurrent calls are not
hypothetical: the gateway and the cron scheduler run side by side, and
``_run_async`` dispatches tool handlers on worker threads.

Contended locks fail fast with ``busy`` rather than queueing. Waiting would
just convert a clear error into a 300s dispatch timeout.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from tools.linkedin.paths import ensure_dirs, lock_path
from tools.linkedin.utils import utc_now_iso


class ProfileBusy(RuntimeError):
    """Raised when another process holds the profile lock."""

    def __init__(self, holder: dict):
        self.holder = holder
        pid = holder.get("pid", "?")
        since = holder.get("acquired_at", "?")
        super().__init__(
            f"LinkedIn profile is in use by PID {pid} (since {since}). "
            "Wait for that run to finish, or kill it if it is stuck."
        )


def _process_alive(pid: int) -> bool:
    """True when *pid* still exists.

    Signal 0 performs the permission and existence checks without delivering
    anything. ``PermissionError`` means the process exists under another
    user, which still counts as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_holder() -> Optional[dict]:
    """Return the current lock holder, or None if unlocked or stale.

    A lockfile whose PID is gone is treated as stale: a crashed run must not
    freeze the funnel until someone deletes a file by hand.
    """
    path = lock_path()
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None

    if not isinstance(holder, dict) or not _process_alive(int(holder.get("pid", 0) or 0)):
        return None
    return holder


class profile_lock:
    """Context manager holding the profile lock for the current process.

    Acquisition is an ``O_CREAT | O_EXCL`` create, which is atomic on POSIX,
    so two processes racing here cannot both win. Only a stale lock is
    reclaimed, and only after the exclusive create has already failed.
    """

    def __init__(self, action: str = ""):
        self.action = action
        self._acquired = False

    def __enter__(self) -> "profile_lock":
        ensure_dirs()
        path = lock_path()
        payload = json.dumps(
            {"pid": os.getpid(), "action": self.action, "acquired_at": utc_now_iso()}
        )

        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = read_holder()
            if holder is not None:
                raise ProfileBusy(holder) from None
            # Stale lock — the writer died. Reclaim it.
            path.unlink(missing_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                # Another process reclaimed it first.
                raise ProfileBusy(read_holder() or {}) from None

        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            lock_path().unlink(missing_ok=True)
            self._acquired = False
