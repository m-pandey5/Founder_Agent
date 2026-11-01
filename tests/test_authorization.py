"""The human-authorization boundary, attacked from every direction we can reach."""

import pytest

from founder_agent.authorization import AuthorizationError, Principal
from founder_agent.config import Settings
from founder_agent.schemas import ActionStatus


def test_approval_token_has_no_default(monkeypatch):
    monkeypatch.delenv("FA_APPROVER_TOKEN", raising=False)
    assert Settings().approver_token is None


def _prepare(system, payload=None):
    return system.gateway.prepare(
        principal=Principal(kind="agent", id="test.agent"),
        tool="external_email.send",
        intent="Test action; must not execute without a human.",
        payload=payload or {"to": "compliance@northbridge.example",
                            "subject": "test", "body": "hello"},
        trace_id="test-trace",
        role="orchestrator",
    )


def test_prepared_action_starts_pending(system):
    action = _prepare(system)
    assert action.status is ActionStatus.PENDING
    assert action.payload_hash


def test_agent_cannot_approve(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="human principal"):
        system.gateway.approve(
            action.action_id,
            Principal(kind="agent", id="orchestrator", token=system.settings.approver_token))
    assert system.gateway.get(action.action_id).status is ActionStatus.PENDING


def test_service_principal_cannot_approve(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError):
        system.gateway.approve(
            action.action_id,
            Principal(kind="service", id="cron", token=system.settings.approver_token))


def test_wrong_credential_is_rejected(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="credential"):
        system.gateway.approve(action.action_id,
                               Principal(kind="human", id="attacker", token="guess"))


def test_execution_without_approval_is_refused(system):
    action = _prepare(system)
    with pytest.raises(AuthorizationError, match="not APPROVED"):
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))


def test_payload_tampering_after_approval_is_refused(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id,
                           Principal(kind="human", id="founder",
                                     token=system.settings.approver_token))
    action.payload["to"] = "attacker@evil.invalid"
    with pytest.raises(AuthorizationError, match="payload changed"):
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))
    assert system.gateway.get(action.action_id).status is ActionStatus.FAILED


def test_approved_action_executes_once_and_records_the_approver(system):
    action = _prepare(system)
    system.gateway.approve(action.action_id,
                           Principal(kind="human", id="founder",
                                     token=system.settings.approver_token))
    done = system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))
    assert done.status is ActionStatus.EXECUTED
    assert done.approved_by == "founder"
    assert done.execution_result["simulated"] is True
    # A second execution is refused: the action is no longer APPROVED.
    with pytest.raises(AuthorizationError):
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))


def test_denied_action_cannot_execute(system):
    action = _prepare(system)
    system.gateway.deny(action.action_id, Principal(kind="human", id="founder"), "no")
    with pytest.raises(AuthorizationError):
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))


def test_expired_action_cannot_be_approved(system, monkeypatch):
    action = _prepare(system)
    from datetime import timedelta
    action.expires_at = action.created_at - timedelta(seconds=1)
    with pytest.raises(AuthorizationError, match="EXPIRED|not pending"):
        system.gateway.approve(action.action_id,
                               Principal(kind="human", id="founder",
                                         token=system.settings.approver_token))


def test_subordinate_agents_hold_no_consequential_capability(system):
    for role in ("research", "redteam", "technical"):
        allowed, reason = system.tools.authorize_call(role, "external_email.send")
        assert not allowed, f"{role} should not reach the email tool"
        assert "not granted" in reason


def test_orchestrator_ask_prepares_but_never_sends(system):
    response = system.orchestrator.ask(
        "Send Northbridge Bank our current custody position", prepare_action=True)
    assert response.pending_action is not None
    assert response.pending_action.status is ActionStatus.PENDING
