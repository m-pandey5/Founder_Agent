"""HTTP authentication, RBAC, and human-only action routes."""

import pytest
from fastapi.testclient import TestClient

READER_TOKEN = "reader-test-token-at-least-32-bytes"
APPROVER_API_TOKEN = "approver-api-test-token-at-least-32"
EXECUTOR_TOKEN = "executor-test-token-at-least-32-bytes"
AUDITOR_TOKEN = "auditor-test-token-at-least-32-bytes"
ADMIN_TOKEN = "admin-test-token-at-least-32-bytes"
APPROVAL_GATE_TOKEN = "approval-gateway-test-token-32-bytes"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    from founder_agent import api
    api.system.settings.api_tokens = {
        "reader": READER_TOKEN,
        "approver": APPROVER_API_TOKEN,
        "executor": EXECUTOR_TOKEN,
        "auditor": AUDITOR_TOKEN,
        "admin": ADMIN_TOKEN,
    }
    api.system.settings.approver_token = APPROVAL_GATE_TOKEN
    return TestClient(api.app)


def test_api_rejects_missing_and_invalid_bearer_credentials(client):
    missing = client.get("/api/health")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/health", headers=auth("wrong-token")).status_code == 401


def test_health_reports_corpus_and_providers(client):
    body = client.get("/api/health", headers=auth(READER_TOKEN)).json()
    assert body["documents"] == 18
    assert body["claims"] > 30
    assert "deterministic" in body["providers"]
    assert body["audit_chain"]["ok"] is True


def test_reader_role_cannot_access_actions_or_audit(client):
    assert client.get("/api/actions", headers=auth(READER_TOKEN)).status_code == 403
    assert client.get("/api/audit/verify", headers=auth(READER_TOKEN)).status_code == 403


def test_auditor_role_cannot_read_corpus(client):
    assert client.get("/api/audit/verify", headers=auth(AUDITOR_TOKEN)).status_code == 200
    assert client.get("/api/documents", headers=auth(AUDITOR_TOKEN)).status_code == 403


def test_ask_returns_cited_segments(client):
    body = client.post(
        "/api/ask",
        json={"question": "What is the signing API rate limit?"},
        headers=auth(READER_TOKEN),
    ).json()
    assert body["answer"]["verdict"] in {"ANSWERED", "PARTIAL"}
    assert all(s["citations"] for s in body["answer"]["segments"])
    assert body["trace_id"]


def test_ask_unknown_question(client):
    body = client.post(
        "/api/ask",
        json={"question": "What is our Series A valuation?"},
        headers=auth(READER_TOKEN),
    ).json()
    assert body["answer"]["verdict"] == "UNKNOWN"


def test_contradictions_endpoint(client):
    body = client.get(
        "/api/contradictions?detail=true", headers=auth(READER_TOKEN)
    ).json()
    assert body["cluster_count"] >= 3
    assert all(c["primary"]["why"] for c in body["clusters"])


def test_action_requires_rbac_and_separate_approval_credential(client):
    prepared = client.post(
        "/api/ask",
        json={"question": "Send Northbridge Bank the position of record",
              "prepare_action": True},
        headers=auth(READER_TOKEN),
    ).json()
    action_id = prepared["pending_action"]["action_id"]

    assert client.post(f"/api/actions/{action_id}/approve").status_code == 401
    assert client.post(
        f"/api/actions/{action_id}/approve", headers=auth(READER_TOKEN)
    ).status_code == 403
    assert client.post(
        f"/api/actions/{action_id}/approve", headers=auth(APPROVER_API_TOKEN)
    ).status_code == 403

    wrong_gateway = {**auth(APPROVER_API_TOKEN), "X-Approver-Token": "wrong"}
    assert client.post(
        f"/api/actions/{action_id}/approve", headers=wrong_gateway
    ).status_code == 403

    approve_headers = {
        **auth(APPROVER_API_TOKEN),
        "X-Approver-Token": APPROVAL_GATE_TOKEN,
    }
    approved = client.post(
        f"/api/actions/{action_id}/approve", headers=approve_headers
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["approved_by"] == "api:approver"

    assert client.post(
        f"/api/actions/{action_id}/execute", headers=auth(APPROVER_API_TOKEN)
    ).status_code == 403
    done = client.post(
        f"/api/actions/{action_id}/execute", headers=auth(EXECUTOR_TOKEN)
    ).json()
    assert done["status"] == "EXECUTED"
    assert done["execution_result"]["simulated"] is True


def test_audit_trace_is_retrievable_only_by_auditor(client):
    body = client.post(
        "/api/ask",
        json={"question": "custody provider policy"},
        headers=auth(READER_TOKEN),
    ).json()
    path = f"/api/audit/trace/{body['trace_id']}"
    assert client.get(path, headers=auth(READER_TOKEN)).status_code == 403
    trace = client.get(path, headers=auth(AUDITOR_TOKEN)).json()
    assert trace["event_count"] >= 3
    assert trace["user_request"]["question"] == "custody provider policy"


def test_security_selftest_is_admin_only(client):
    assert client.post(
        "/api/security/selftest", headers=auth(AUDITOR_TOKEN)
    ).status_code == 403
    body = client.post(
        "/api/security/selftest", headers=auth(ADMIN_TOKEN)
    ).json()
    failed = [c for c in body["checks"] if not c["passed"]]
    assert not failed, failed
    assert body["all_passed"] is True


def test_ui_is_served_without_embedding_a_credential(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "FOUNDER_AGENT" in response.text
    assert "dev-human-console-token" not in response.text
