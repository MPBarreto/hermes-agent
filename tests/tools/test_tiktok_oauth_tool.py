"""Tests for TikTok API for Business OAuth helper."""

from __future__ import annotations

import json
import stat
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest

from tools import tiktok_oauth_tool as tool


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def tiktok_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(tool, "_redirect_uri", lambda: "https://example.com/tiktok/callback/")
    monkeypatch.setattr(tool, "_configured_scopes", lambda: ["user.info.basic", "video.list"])
    return tmp_path


@pytest.mark.parametrize(
    ("uri", "valid", "reason"),
    [
        ("https://example.com/callback/", True, ""),
        ("http://example.com/callback/", False, "https://"),
        ("https://example.com:8443/callback/", False, "port"),
        ("https://example.com/callback", False, "end with"),
        ("https://example.com/callback/?x=1", False, "query"),
        ("https://example.com/callback/#fragment", False, "fragment"),
        ("/callback/", False, "https://"),
    ],
)
def test_validate_redirect_uri(uri, valid, reason):
    result, message = tool.validate_redirect_uri(uri)
    assert result is valid
    if reason:
        assert reason in message


def test_authorize_builds_url_and_persists_state(tiktok_env):
    result = json.loads(tool._handle({"action": "authorize"}))

    assert result["status"] == "ok"
    parsed = urlsplit(result["authorization_url"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.tiktok.com"
    assert query["client_key"] == ["client-key"]
    assert query["redirect_uri"] == ["https://example.com/tiktok/callback/"]
    assert query["scope"] == ["user.info.basic video.list"]
    assert query["state"] == [result["state"]]
    assert (tiktok_env / "tiktok" / "oauth-state.json").exists()


def test_authorize_rejects_unusable_redirect(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "client-key")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(tool, "_redirect_uri", lambda: "http://localhost:8080/callback")
    result = json.loads(tool._handle({"action": "authorize", "scope": "user.info.basic"}))
    assert result["status"] == "error"
    assert "https://" in result["message"]


def test_exchange_accepts_callback_url_and_stores_owner_only_tokens(tiktok_env):
    authorize = json.loads(tool._handle({"action": "authorize", "scope": "user.info.basic"}))
    callback = "https://example.com/tiktok/callback/?code=auth-code&state=" + authorize["state"]
    payload = {
        "code": 0,
        "message": "OK",
        "data": {
            "access_token": "access-secret",
            "expires_in": 3600,
            "refresh_token": "refresh-secret",
            "refresh_expires_in": 86400,
            "open_id": "open-id",
            "advertiser_ids": ["123"],
        },
    }
    with patch.object(tool.httpx, "post", return_value=FakeResponse(payload)) as post:
        result = json.loads(tool._handle({"action": "exchange_code", "callback_url": callback}))

    assert result["status"] == "ok"
    sent = post.call_args.kwargs["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["auth_code"] == "auth-code"
    assert sent["client_secret"] == "client-secret"
    token_path = tiktok_env / "tiktok" / "tokens.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    stored = json.loads(token_path.read_text())
    assert stored["access_token"] == "access-secret"
    assert json.loads(tool._handle({"action": "status"}))["access_token_present"] is True


def test_exchange_rejects_state_mismatch_without_network(tiktok_env):
    tool._write_json(tool._state_path(), {"state": "expected"})
    with patch.object(tool.httpx, "post") as post:
        result = json.loads(
            tool._handle(
                {
                    "action": "exchange_code",
                    "code": "auth-code",
                    "callback_state": "wrong",
                }
            )
        )
    assert result["status"] == "error"
    assert "state mismatch" in result["message"]
    post.assert_not_called()


def test_refresh_uses_stored_refresh_token(tiktok_env):
    tool._write_json(
        tool._token_path(),
        {"refresh_token": "refresh-secret", "access_token": "old", "expires_at": 1},
    )
    payload = {"code": 0, "data": {"access_token": "new-access", "expires_in": 100}}
    with patch.object(tool.httpx, "post", return_value=FakeResponse(payload)) as post:
        result = json.loads(tool._handle({"action": "refresh"}))

    assert result["status"] == "ok"
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert post.call_args.kwargs["data"]["refresh_token"] == "refresh-secret"
    assert json.loads(tool._handle({"action": "status"}))["expired"] is False


def test_missing_credentials_are_actionable(monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    result = json.loads(tool._handle({"action": "authorize"}))
    assert result["status"] == "error"
    assert "TIKTOK_CLIENT_KEY" in result["message"]
    assert "TIKTOK_CLIENT_SECRET" in result["message"]


def test_logout_removes_tokens(tiktok_env):
    tool._write_json(tool._token_path(), {"access_token": "secret"})
    result = json.loads(tool._handle({"action": "logout"}))
    assert result == {"status": "ok", "action": "logout", "removed": True}
    assert not tool._token_path().exists()
