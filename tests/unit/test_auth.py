"""API keys and JWT/OIDC bearer tokens (ADR 017), against a fake identity provider."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.auth.api_key import Authenticator, hash_key
from chatbot.auth.jwt import JwtVerifier, TokenError, sign_dev_token
from chatbot.core import container
from chatbot.core.settings import ConfigError, JwtSection
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore
from tests.conftest import offline_settings

ISSUER = "https://idp.example.com/"
AUDIENCE = "chatbot-api"
SECRET = "s" * 40


class FakeIdp:
    """Serves OIDC discovery and a JWKS, and signs tokens. Keys can be rotated."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.requests: list[str] = []
        self.rotate()

    def rotate(self) -> str:
        kid = f"k{len(self.keys) + 1}"
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.current = kid
        return kid

    def jwks(self) -> dict:
        out = []
        for kid, key in self.keys.items():
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": out}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200, json={"issuer": ISSUER, "jwks_uri": "https://idp.example.com/jwks"}
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def token(self, sub="user-1", kid=None, **claims) -> str:
        now = int(time.time())
        body = {"sub": sub, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not None}
        kid = kid or self.current
        return jwt.encode(body, self.keys[kid], algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def verifier(idp: FakeIdp, **cfg) -> JwtVerifier:
    section = JwtSection(issuer=ISSUER, audience=AUDIENCE, **cfg)
    return JwtVerifier(section, idp.client())


# ---------------------------------------------------------------- verifier


async def test_valid_token_via_oidc_discovery(idp):
    principal = await verifier(idp).verify(idp.token(scope="read chatbot:admin"))
    assert principal.method == "jwt" and principal.subject == "user-1"
    assert principal.admin and principal.scopes == {"read", "chatbot:admin"}
    assert idp.requests == ["/.well-known/openid-configuration", "/jwks"]


async def test_keys_are_cached_and_rotation_is_picked_up(idp):
    v = verifier(idp)
    await v.verify(idp.token())
    await v.verify(idp.token())
    assert idp.requests.count("/jwks") == 1
    idp.rotate()
    v._fetched_at -= 60  # past the minimum refresh interval
    assert (await v.verify(idp.token())).subject == "user-1"
    assert idp.requests.count("/jwks") == 2


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"aud": "someone-else"}, "InvalidAudienceError"),
        ({"iss": "https://evil.example/"}, "InvalidIssuerError"),
        ({"exp": None}, "MissingRequiredClaimError"),
        ({"sub": None}, "no 'sub' claim"),
        ({"nbf": int(time.time()) + 600}, "ImmatureSignatureError"),
    ],
)
async def test_bad_claims_are_rejected(idp, claims, message):
    with pytest.raises(TokenError, match=message):
        await verifier(idp).verify(idp.token(**claims))


async def test_forged_and_unsigned_tokens_are_rejected(idp):
    v = verifier(idp)
    other = FakeIdp()
    forged = other.token(kid="k1")  # same kid, different key
    with pytest.raises(TokenError, match="InvalidSignatureError"):
        await v.verify(forged)
    unsigned = jwt.encode({"sub": "x", "iss": ISSUER, "aud": AUDIENCE}, None, algorithm="none")
    with pytest.raises(TokenError, match="not allowed"):
        await v.verify(unsigned)
    # An HS256 token signed with the public key (algorithm confusion) is refused too.
    hs = jwt.encode({"sub": "x"}, "anything-at-all-long-enough-0123456789", algorithm="HS256")
    with pytest.raises(TokenError, match="not allowed"):
        await v.verify(hs)
    with pytest.raises(TokenError, match="malformed"):
        await v.verify("not-a-token")


async def test_required_scopes(idp):
    v = verifier(idp, required_scopes=["chat"])
    assert (await v.verify(idp.token(scp=["chat", "x"]))).subject == "user-1"
    with pytest.raises(TokenError, match="required scopes"):
        await v.verify(idp.token(scope="other"))


async def test_custom_user_id_claim(idp):
    v = verifier(idp, user_id_claim="email")
    assert (await v.verify(idp.token(email="a@b.co"))).subject == "a@b.co"


async def test_hs256_dev_tokens(monkeypatch):
    monkeypatch.setenv("DEV_JWT_SECRET", SECRET)
    section = JwtSection(
        issuer=ISSUER, audience=AUDIENCE, algorithms=["HS256"], hs256_secret_env="DEV_JWT_SECRET"
    )
    token = sign_dev_token(SECRET, "dev-user", ISSUER, AUDIENCE, scopes=["chatbot:admin"])
    principal = await JwtVerifier(section).verify(token)
    assert principal.subject == "dev-user" and principal.admin
    bad = sign_dev_token("x" * 40, "dev-user", ISSUER, AUDIENCE)
    with pytest.raises(TokenError, match="InvalidSignatureError"):
        await JwtVerifier(section).verify(bad)


def test_jwt_config_validation():
    with pytest.raises(ConfigError, match=r"needs auth\.jwt\.audience"):
        offline_settings(auth={"mode": "jwt", "jwt": {"issuer": ISSUER}})
    with pytest.raises(ConfigError, match="unsupported JWT algorithms"):
        offline_settings(auth={"mode": "none", "jwt": {"algorithms": ["none"]}})
    with pytest.raises(ConfigError, match="HS256 needs hs256_secret_env"):
        offline_settings(auth={"mode": "none", "jwt": {"algorithms": ["HS256"]}})


# ---------------------------------------------------------------- the API


def jwt_settings(mode: str = "jwt"):
    return offline_settings(
        auth={"mode": mode, "jwt": {"issuer": ISSUER, "audience": AUDIENCE}},
        memory={"run_in": "inline"},
    )


def fakes(settings):
    return container.build(
        settings,
        pool=None,
        answer_llm=FakeLLM(),
        cheap_llm=FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
    )


@pytest.fixture
def api(idp, monkeypatch):
    key = "cb_test_key"
    monkeypatch.setenv("CHATBOT_API_KEYS", hash_key(key))
    settings = jwt_settings("api_key_or_jwt")
    auth = Authenticator(settings.auth, idp.client())
    app = create_app(settings, components=fakes(settings), auth=auth)
    with TestClient(app) as client:
        client.key = key
        client.idp = idp
        yield client


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_both_credentials_work(api):
    assert api.get("/v1/sources", headers={"X-API-Key": api.key}).status_code == 200
    assert api.get("/v1/sources", headers=bearer(api.key)).status_code == 200
    admin = api.idp.token(scope="chatbot:admin")
    assert api.get("/v1/sources", headers=bearer(admin)).status_code == 200
    assert api.get("/v1/sources").status_code == 401
    assert api.get("/v1/sources", headers={"X-API-Key": "wrong"}).status_code == 401
    resp = api.get("/v1/sources", headers=bearer(api.idp.token(aud="x")))
    assert resp.status_code == 401 and resp.headers["www-authenticate"] == "Bearer"


def test_end_users_chat_as_themselves(api):
    token = bearer(api.idp.token(sub="alice"))
    resp = api.post("/v1/chat", json={"message": "How much is Plus?"}, headers=token)
    assert resp.status_code == 200
    conv = resp.json()["conversation_id"]
    assert api.app.state.components.memory.conversations[conv] == "alice"

    resp = api.post("/v1/chat", json={"message": "hi", "user_id": "bob"}, headers=token)
    assert resp.status_code == 403
    # Continuing someone else's conversation is refused by the memory owner check.
    bob = bearer(api.idp.token(sub="bob"))
    resp = api.post("/v1/chat", json={"message": "hi", "conversation_id": conv}, headers=bob)
    assert resp.status_code == 403
    # A backend with an API key may act for any user.
    resp = api.post(
        "/v1/chat",
        json={"message": "hi", "user_id": "bob"},
        headers={"X-API-Key": api.key},
    )
    assert resp.status_code == 200


def test_end_users_manage_only_their_own_memory(api):
    alice = bearer(api.idp.token(sub="alice"))
    api.post("/v1/chat", json={"message": "I use the Team plan. How much is Plus?"}, headers=alice)
    facts = api.get("/v1/users/alice/memory", headers=alice).json()["facts"]
    assert [f["fact"] for f in facts] == ["The user said: I use the Team plan."]
    assert api.get("/v1/users/bob/memory", headers=alice).status_code == 403
    assert api.delete("/v1/users/bob/memory", headers=alice).status_code == 403

    fact_id = facts[0]["id"]
    assert api.delete(f"/v1/users/alice/memory/facts/{fact_id}", headers=alice).status_code == 204
    assert api.delete(f"/v1/users/alice/memory/facts/{fact_id}", headers=alice).status_code == 404
    report = api.delete("/v1/users/alice/memory", headers=alice).json()
    assert report["deleted"]["turns"] == 2


def test_end_users_cannot_reach_ingestion(api):
    alice = bearer(api.idp.token(sub="alice"))
    assert api.post("/v1/ingest", json={"source": "samples"}, headers=alice).status_code == 403
    assert api.get("/v1/sources", headers=alice).status_code == 403
    assert api.delete("/v1/documents/x", headers=alice).status_code == 403


def test_jwt_only_mode_refuses_api_keys(idp, monkeypatch):
    monkeypatch.setenv("CHATBOT_API_KEYS", hash_key("cb_k"))
    settings = jwt_settings("jwt")
    app = create_app(settings, components=fakes(settings), auth=Authenticator(settings.auth))
    with TestClient(app) as client:
        assert client.get("/v1/sources", headers={"X-API-Key": "cb_k"}).status_code == 401
        assert client.get("/healthz").status_code == 200
