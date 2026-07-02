"""Tests for the Instagram platform adapter.

Focus: the adapter must honour the BasePlatformAdapter.connect contract,
which the gateway always calls as ``connect(is_reconnect=...)``. A missing
``is_reconnect`` parameter raised ``TypeError`` on the reconnect path and
kept Instagram from reconnecting after an outage.

Also covers the unconfigured/missing-token path (fatal, no crash) and the
outbound send contract.
"""

import inspect
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.instagram.adapter import (
    InstagramAdapter,
    check_instagram_requirements,
)


TOKEN_ENV = {"IG_ACCESS_TOKEN": "test-token", "IG_USER_ID": "123"}


def _make_adapter(extra=None):
    return InstagramAdapter(PlatformConfig(enabled=True, extra=extra or {}))


@asynccontextmanager
async def _fake_get(status=200, payload=None):
    """A stand-in for session.get(...) as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload or {"id": "123", "username": "afiro.ai"})
    yield resp


def _mock_session(get_status=200, get_payload=None):
    """Build a mock aiohttp.ClientSession whose .get() returns _fake_get()."""
    session = MagicMock()
    session.get = MagicMock(return_value=_fake_get(get_status, get_payload))
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Signature contract
# ---------------------------------------------------------------------------

def test_connect_signature_matches_base():
    """Adapter.connect must accept the keyword-only is_reconnect like the base."""
    base_sig = inspect.signature(BasePlatformAdapter.connect)
    ig_sig = inspect.signature(InstagramAdapter.connect)
    assert "is_reconnect" in ig_sig.parameters
    p = ig_sig.parameters["is_reconnect"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default is False
    # keep parity with the base contract
    assert "is_reconnect" in base_sig.parameters


# ---------------------------------------------------------------------------
# connect(is_reconnect=...) — both values must work (the reconnect TypeError)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("is_reconnect", [False, True])
async def test_connect_accepts_is_reconnect(is_reconnect):
    with patch.dict(os.environ, TOKEN_ENV, clear=False):
        adapter = _make_adapter()
        session = _mock_session()
        with patch("aiohttp.ClientSession", return_value=session):
            ok = await adapter.connect(is_reconnect=is_reconnect)
        assert ok is True
        assert adapter._running is True
        await adapter.disconnect()


# ---------------------------------------------------------------------------
# Missing token / bad token — fatal, returns False, does not raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_without_token_returns_false():
    env = {k: v for k, v in os.environ.items() if k != "IG_ACCESS_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        adapter = _make_adapter()
        ok = await adapter.connect(is_reconnect=True)
        assert ok is False
        assert adapter._running is False


@pytest.mark.asyncio
async def test_connect_with_bad_token_returns_false():
    with patch.dict(os.environ, TOKEN_ENV, clear=False):
        adapter = _make_adapter()
        session = _mock_session(
            get_status=400, get_payload={"error": {"message": "Invalid OAuth token"}}
        )
        with patch("aiohttp.ClientSession", return_value=session):
            ok = await adapter.connect(is_reconnect=False)
        assert ok is False
        assert adapter._running is False
        session.close.assert_awaited()


def test_check_requirements_needs_token():
    env = {k: v for k, v in os.environ.items() if k != "IG_ACCESS_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        assert check_instagram_requirements() is False
    with patch.dict(os.environ, TOKEN_ENV, clear=False):
        assert check_instagram_requirements() is True


# ---------------------------------------------------------------------------
# send() outbound contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_posts_to_graph_and_returns_message_id():
    with patch.dict(os.environ, TOKEN_ENV, clear=False):
        adapter = _make_adapter()

        post_resp = MagicMock()
        post_resp.status = 200
        post_resp.json = AsyncMock(
            return_value={"recipient_id": "999", "message_id": "mid_abc"}
        )

        @asynccontextmanager
        async def _fake_post(*a, **k):
            yield post_resp

        session = MagicMock()
        session.post = MagicMock(return_value=_fake_post())
        session.close = AsyncMock()
        adapter._http_session = session

        result = await adapter.send("999", "olá")
        assert result.success is True
        assert result.message_id == "mid_abc"


@pytest.mark.asyncio
async def test_send_empty_content_is_noop():
    with patch.dict(os.environ, TOKEN_ENV, clear=False):
        adapter = _make_adapter()
        result = await adapter.send("999", "   ")
        assert result.success is True
        assert result.message_id is None
