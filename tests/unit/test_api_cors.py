"""Browsers on another domain can call the API once their origin is listed (api.cors).

A browser first sends a preflight (OPTIONS) asking whether the real request is allowed; it
carries no credentials, so it must be answered before auth. Error responses need the CORS
headers too, or the browser hides the 401 from the app.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.auth.api_key import hash_key
from chatbot.core.settings import ConfigError
from tests.conftest import offline_settings

APP = "https://app.example.com"
KEY = "cb_cors_test_key_0123456789"


def client(monkeypatch, fake_components, **api) -> TestClient:
    monkeypatch.setenv("CHATBOT_API_KEYS", hash_key(KEY))
    settings = offline_settings(auth={"mode": "api_key"}, api=api)
    return TestClient(create_app(settings, components=fake_components))


def preflight(c: TestClient, origin: str = APP):
    return c.options(
        "/v1/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )


def test_cors_is_off_by_default(monkeypatch, fake_components):
    with client(monkeypatch, fake_components) as c:
        assert "access-control-allow-origin" not in preflight(c).headers


def test_listed_origin_can_call_the_api(monkeypatch, fake_components):
    with client(monkeypatch, fake_components, cors={"allow_origins": [APP]}) as c:
        pre = preflight(c)
        assert pre.status_code == 200  # answered before auth: no key on a preflight
        assert pre.headers["access-control-allow-origin"] == APP
        assert "authorization" in pre.headers["access-control-allow-headers"].lower()

        resp = c.post(
            "/v1/chat",
            json={"message": "How long is the refund window?"},
            headers={"Origin": APP, "Authorization": f"Bearer {KEY}"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == APP
        assert "x-trace-id" in resp.headers["access-control-expose-headers"].lower()

        # The app must see why it failed, not a generic CORS error.
        denied = c.post("/v1/chat", json={"message": "hi"}, headers={"Origin": APP})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == APP


def test_other_origins_are_refused(monkeypatch, fake_components):
    with client(monkeypatch, fake_components, cors={"allow_origins": [APP]}) as c:
        assert "access-control-allow-origin" not in preflight(c, "https://evil.example").headers


def test_wildcard_with_credentials_is_rejected():
    with pytest.raises(ConfigError, match="allow_credentials"):
        offline_settings(api={"cors": {"allow_origins": ["*"], "allow_credentials": True}})


def test_api_docs_can_be_turned_off(monkeypatch, fake_components):
    with client(monkeypatch, fake_components) as c:
        assert c.get("/openapi.json").status_code == 200
    with client(monkeypatch, fake_components, docs=False) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404
