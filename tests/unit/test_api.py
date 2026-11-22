import json

import pytest
from fastapi.testclient import TestClient

from chatbot.api.app import create_app
from chatbot.auth.api_key import hash_key
from chatbot.ingestion.jobs.handlers import make_handlers
from chatbot.ingestion.jobs.worker import Worker
from tests.conftest import offline_settings


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def client(fake_components):
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as c:
        c.components = fake_components
        yield c


def ingest_samples(client) -> None:
    resp = client.post("/v1/ingest", json={"source": "samples"})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    c = client.components
    client.portal.call(Worker(c.queue, make_handlers(c)).drain)
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    assert len(job["result"]["indexed"]) == 8


def test_health_and_trace_header(client):
    resp = client.get("/healthz", headers={"X-Request-Id": "abcdef123456"})
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == "abcdef123456"


def test_ingest_then_chat_json(client):
    ingest_samples(client)
    resp = client.post("/v1/chat", json={"message": "Where are notes stored by default?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "answer" and "Frankfurt" in body["answer"]
    assert body["citations"][0]["document_id"] == "samples:privacy-and-data.html"
    assert body["trace_id"] == resp.headers["x-trace-id"]


def test_chat_sse_contract(client):
    ingest_samples(client)
    resp = client.post(
        "/v1/chat",
        json={"message": "How many people can edit the same note at once?", "stream": True},
    )
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert events[-1][0] == "final"
    assert all(name == "delta" for name, _ in events[:-1])
    assert events[-1][1]["citations"][0]["document_id"] == "samples:sharing-and-collaboration.md"


def test_inline_documents_and_sources(client):
    resp = client.post(
        "/v1/ingest",
        json={
            "source": "crm",
            "user_id": "u9",
            "documents": [{"id": "42", "title": "Order 42", "text": "Order 42 ships on Friday."}],
        },
    )
    assert resp.status_code == 202
    c = client.components
    client.portal.call(Worker(c.queue, make_handlers(c)).drain)
    sources = client.get("/v1/sources").json()
    assert sources["index"]["embedding_model"] == "hashing-v1"
    resp = client.post("/v1/chat", json={"message": "When does order 42 ship?"})
    assert resp.json()["citations"][0]["document_id"] == "crm:42"
    assert client.delete("/v1/documents/crm:42").status_code == 200
    assert client.delete("/v1/documents/crm:42").status_code == 404


def test_unknown_source_is_404(client):
    assert client.post("/v1/ingest", json={"source": "nope"}).status_code == 404
    assert client.post("/v1/ingest", json={}).status_code == 422


def test_forget_user(client):
    ingest_samples(client)
    client.post("/v1/chat", json={"message": "Where are notes stored?", "user_id": "u1"})
    report = client.delete("/v1/users/u1/memory").json()
    assert report["deleted"]["turns"] == 2


def test_forget_user_also_erases_vectors_kept_outside_postgres(fake_components):
    erased = []

    class ErasingStore(type(fake_components.store)):
        async def forget_user_documents(self, user_id: str) -> int:
            erased.append(user_id)
            return 3

    fake_components.store = ErasingStore()
    app = create_app(fake_components.settings, components=fake_components)
    with TestClient(app) as c:
        report = c.delete("/v1/users/u9/memory").json()
    assert erased == ["u9"] and report["deleted"]["vector_points"] == 3


def test_wrong_owner_gets_403(client):
    ingest_samples(client)
    first = client.post("/v1/chat", json={"message": "Where are notes stored?", "user_id": "a"})
    cid = first.json()["conversation_id"]
    resp = client.post("/v1/chat", json={"message": "hi", "user_id": "b", "conversation_id": cid})
    assert resp.status_code == 403


def test_api_key_auth(fake_components, monkeypatch):
    monkeypatch.setenv("CHATBOT_API_KEYS", hash_key("secret-key"))
    settings = offline_settings(auth={"mode": "api_key"})
    app = create_app(settings, components=fake_components)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # health is public
        assert c.get("/v1/sources").status_code == 401
        assert c.get("/v1/sources", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/v1/sources", headers={"X-API-Key": "secret-key"}).status_code == 200
        ok = c.get("/v1/sources", headers={"Authorization": "Bearer secret-key"})
        assert ok.status_code == 200


def test_api_key_mode_without_keys_fails_fast(fake_components, monkeypatch):
    monkeypatch.delenv("CHATBOT_API_KEYS", raising=False)
    with pytest.raises(RuntimeError, match="CHATBOT_API_KEYS"):
        create_app(offline_settings(auth={"mode": "api_key"}), components=fake_components)
