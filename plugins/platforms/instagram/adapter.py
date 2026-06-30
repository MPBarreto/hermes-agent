"""Instagram (Instagram Login / Graph API) platform adapter.

Outbound-focused adapter for the Instagram Messaging API, using the
*Instagram API with Instagram Login* product (host ``graph.instagram.com``,
**not** ``graph.facebook.com``).  It sends/replies to Direct Messages via:

    POST https://graph.instagram.com/<api_version>/me/messages
    { "recipient": {"id": "<IGSID>"}, "message": {"text": "..."} }

Inbound DMs/comments are already received by the generic ``webhook`` platform
(see ``gateway/platforms/webhook.py`` + ``hermes webhook subscribe``), which
delivers them to Slack today.  This adapter provides the *send* side so the
agent can reply on Instagram.  It is therefore registered primarily as a
cross-platform delivery target and a ``send_message`` destination.

Auth: a long-lived Instagram user access token (~60 days, refreshable via
``graph.instagram.com/refresh_access_token``).  Store it in ~/.hermes/.env.

Env vars:
  - IG_ACCESS_TOKEN   (required) long-lived Instagram user token
  - IG_USER_ID        (optional) the account's Graph id — used for /me checks
  - IG_API_VERSION    (optional) Graph API version, default v23.0
  - IG_ALLOWED_USERS  (optional) comma-separated IGSIDs allowed to talk to bot
  - IG_ALLOW_ALL_USERS(optional) true/false
  - IG_HOME_CHANNEL   (optional) default IGSID for cron / notification delivery

Messaging window: Instagram only allows sending a message to a user within
24h of their last message (the "standard messaging window"), unless using a
message tag.  Outside the window the Graph API returns an error, which this
adapter surfaces verbatim.
"""

import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.instagram.com"
DEFAULT_API_VERSION = "v23.0"
# Instagram DM text limit is 1000 chars; keep a small safety margin.
MAX_IG_MESSAGE_LENGTH = 1000


def check_instagram_requirements() -> bool:
    """Instagram adapter needs aiohttp and a configured access token."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("IG_ACCESS_TOKEN"))


class InstagramAdapter(BasePlatformAdapter):
    """Instagram Direct Message sender via the Graph API (Instagram Login).

    ``chat_id`` is the recipient's IGSID (Instagram-scoped user id) — the
    ``sender.id`` value Meta puts in the inbound webhook payload.
    """

    MAX_MESSAGE_LENGTH = MAX_IG_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("instagram"))
        extra = config.extra or {}
        self._access_token: str = str(
            extra.get("access_token") or os.getenv("IG_ACCESS_TOKEN", "")
        ).strip()
        self._user_id: str = str(
            extra.get("user_id") or os.getenv("IG_USER_ID", "")
        ).strip()
        self._api_version: str = str(
            extra.get("api_version") or os.getenv("IG_API_VERSION", DEFAULT_API_VERSION)
        ).strip()
        self._http_session: Optional["aiohttp.ClientSession"] = None

    # ------------------------------------------------------------------ helpers
    def _graph_url(self, path: str = "messages") -> str:
        path = path.lstrip("/")
        return f"{GRAPH_API_BASE}/{self._api_version}/me/{path}"

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> bool:
        import aiohttp

        if not self._access_token:
            msg = "[instagram] IG_ACCESS_TOKEN not set — cannot send messages"
            logger.error(msg)
            self._set_fatal_error(
                "instagram_unconfigured", msg, retryable=False
            )
            return False

        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )

        # Validate the token up front so misconfiguration surfaces at connect
        # time rather than on the first reply attempt.
        try:
            url = f"{GRAPH_API_BASE}/{self._api_version}/me"
            params = {"fields": "id,username", "access_token": self._access_token}
            async with self._http_session.get(url, params=params) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    err = (data.get("error") or {}).get("message", str(data))
                    msg = f"[instagram] token validation failed: {err}"
                    logger.error(msg)
                    self._set_fatal_error(
                        "instagram_bad_token", msg, retryable=False
                    )
                    await self._http_session.close()
                    self._http_session = None
                    return False
                username = data.get("username", "?")
                logger.info(
                    "[instagram] Connected as @%s (Graph %s)",
                    username,
                    self._api_version,
                )
        except Exception as e:
            logger.exception("[instagram] connect/token check failed")
            self._set_fatal_error("instagram_connect_error", str(e), retryable=True)
            if self._http_session:
                await self._http_session.close()
                self._http_session = None
            return False

        self._mark_connected()
        self._running = True
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._running = False
        self._mark_disconnected()
        logger.info("[instagram] Disconnected")

    # ------------------------------------------------------------------ outbound
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Direct Message to ``chat_id`` (the recipient's IGSID)."""
        import aiohttp

        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        last_result = SendResult(success=True)
        try:
            for chunk in chunks:
                payload = {
                    "recipient": {"id": str(chat_id)},
                    "message": {"text": chunk},
                }
                try:
                    async with session.post(
                        url, headers=headers, json=payload
                    ) as resp:
                        body = await resp.json()
                        if resp.status >= 400:
                            err = (body.get("error") or {}).get(
                                "message", str(body)
                            )
                            logger.warning(
                                "[instagram] send rejected (status=%d): %s",
                                resp.status,
                                err,
                            )
                            return SendResult(
                                success=False,
                                error=f"Instagram {resp.status}: {err}",
                            )
                        # Successful send returns {recipient_id, message_id}.
                        last_result = SendResult(
                            success=True,
                            message_id=body.get("message_id"),
                        )
                except Exception as e:
                    logger.exception("[instagram] send error")
                    return SendResult(success=False, error=str(e))
        finally:
            if not self._http_session and session:
                await session.close()

        return last_result

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}


# ──────────────────────────────────────────────────────────────────────────
# Plugin registration
# ──────────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
    subject=None,
    **_ignored,
):
    """Out-of-process Instagram DM delivery via the Graph API.

    Implements the ``standalone_sender_fn`` contract used by cron jobs and
    the ``send_message`` tool when the gateway adapter is not in-process.
    """
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    token = os.getenv("IG_ACCESS_TOKEN", "").strip()
    api_version = os.getenv("IG_API_VERSION", DEFAULT_API_VERSION).strip()
    if not token:
        return {"error": "Instagram not configured (IG_ACCESS_TOKEN required)"}

    url = f"{GRAPH_API_BASE}/{api_version}/me/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"recipient": {"id": str(chat_id)}, "message": {"text": message}}
    try:
        from gateway.platforms.base import (
            resolve_proxy_url,
            proxy_kwargs_for_aiohttp,
        )

        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), **_sess_kw
        ) as session:
            async with session.post(
                url, headers=headers, json=payload, **_req_kw
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    err = (body.get("error") or {}).get("message", str(body))
                    return {"error": f"Instagram API error ({resp.status}): {err}"}
                return {
                    "success": True,
                    "platform": "instagram",
                    "chat_id": chat_id,
                    "message_id": body.get("message_id", ""),
                }
    except Exception as e:
        return {"error": f"Instagram send failed: {e}"}


def _is_connected(config) -> bool:
    """Instagram is connected when an access token is present."""
    import hermes_cli.gateway as gateway_mod

    return bool((gateway_mod.get_env_value("IG_ACCESS_TOKEN") or "").strip())


def _build_adapter(config):
    """Factory wrapper that constructs InstagramAdapter from a PlatformConfig."""
    return InstagramAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="instagram",
        label="Instagram (Direct Messages)",
        adapter_factory=_build_adapter,
        check_fn=check_instagram_requirements,
        is_connected=_is_connected,
        required_env=["IG_ACCESS_TOKEN"],
        install_hint="pip install aiohttp",
        allowed_users_env="IG_ALLOWED_USERS",
        allow_all_env="IG_ALLOW_ALL_USERS",
        cron_deliver_env_var="IG_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_IG_MESSAGE_LENGTH,
        pii_safe=False,
        emoji="📸",
        allow_update_command=True,
    )
