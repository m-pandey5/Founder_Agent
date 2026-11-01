"""Human-in-the-loop gate for consequential actions (assessment 2.6).

The design point: *the agent cannot approve its own action, and no prompt can
make it able to.* Approval is a capability held by a human principal, checked
against a token the agent process is never given, in a code path the agent
cannot reach.

Three properties the gateway enforces:

1. **Separation.** `prepare()` is callable by an agent. `approve()` rejects any
   principal that is not human, before it looks at anything else.
2. **Binding.** Approval is bound to a hash of the exact payload the human saw.
   If the payload changes afterwards, execution fails - an agent cannot get a
   benign draft approved and then swap in a different one.
3. **Expiry.** A pending action goes stale. Stale approval is no approval.
"""

from __future__ import annotations

import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from .audit import AuditLog
from .config import Settings, settings as default_settings
from .schemas import ActionRequest, ActionStatus, canonical_hash, utcnow
from .tools.registry import ToolRegistry


@dataclass(frozen=True)
class Principal:
    """Who is acting. `kind` is the security boundary, not `id`."""

    kind: Literal["human", "agent", "service"]
    id: str
    token: str | None = None


class AuthorizationError(PermissionError):
    pass


class ActionGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        audit: AuditLog,
        settings: Settings | None = None,
        store_path: Path | None = None,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self.settings = settings or default_settings
        self.store_path = store_path or self.settings.actions_path
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._actions: dict[str, ActionRequest] = {}
        self._load()

    # -- step 1: an agent prepares -----------------------------------------
    def prepare(
        self,
        *,
        principal: Principal,
        tool: str,
        intent: str,
        payload: dict[str, Any],
        trace_id: str,
        supporting_claim_ids: list[str] | None = None,
        warnings: list[str] | None = None,
        role: str = "orchestrator",
    ) -> ActionRequest:
        allowed, reason = self.registry.authorize_call(role, tool)
        if not allowed:
            self.audit.record("action_refused", trace_id=trace_id, actor=principal.id,
                              payload={"tool": tool, "reason": reason})
            raise AuthorizationError(reason)

        spec = self.registry.get(tool)
        assert spec is not None
        action = ActionRequest(
            action_id=f"act_{uuid.uuid4().hex[:12]}",
            trace_id=trace_id,
            tool=tool,
            risk=spec.risk,
            intent=intent,
            payload=payload,
            payload_hash=canonical_hash(payload),
            status=ActionStatus.PENDING,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(seconds=self.settings.action_ttl_seconds),
            requested_by=principal.id,
            supporting_claim_ids=supporting_claim_ids or [],
            warnings=warnings or [],
        )
        self._persist(action)
        self.audit.record("action_prepared", trace_id=trace_id, actor=principal.id, payload={
            "action_id": action.action_id, "tool": tool, "risk": spec.risk, "intent": intent,
            "payload_hash": action.payload_hash, "payload": payload,
            "requires_human_approval": spec.requires_human_approval,
            "supporting_claim_ids": action.supporting_claim_ids,
            "warnings": action.warnings,
        })
        return action

    # -- step 2: a human decides -------------------------------------------
    def approve(self, action_id: str, principal: Principal, note: str = "") -> ActionRequest:
        # The boundary check comes first, deliberately: an agent principal is
        # rejected before the gateway even considers the action's contents.
        if principal.kind != "human":
            self._deny_attempt(action_id, principal, "non-human principal attempted approval")
            raise AuthorizationError("only a human principal may approve an action")
        if not self._valid_token(principal.token):
            self._deny_attempt(action_id, principal, "invalid approver credential")
            raise AuthorizationError("invalid approver credential")

        action = self._require(action_id)
        self._expire_if_stale(action)
        if action.status is not ActionStatus.PENDING:
            raise AuthorizationError(f"action is {action.status.value}, not pending")

        action.status = ActionStatus.APPROVED
        action.approved_by = principal.id
        action.approved_at = utcnow()
        self._persist(action)
        self.audit.record("action_approved", trace_id=action.trace_id, actor=principal.id, payload={
            "action_id": action_id, "tool": action.tool,
            "payload_hash": action.payload_hash, "note": note,
        })
        return action

    def deny(self, action_id: str, principal: Principal, reason: str) -> ActionRequest:
        if principal.kind != "human":
            raise AuthorizationError("only a human principal may deny an action")
        action = self._require(action_id)
        action.status = ActionStatus.DENIED
        action.denial_reason = reason
        self._persist(action)
        self.audit.record("action_denied", trace_id=action.trace_id, actor=principal.id,
                          payload={"action_id": action_id, "reason": reason})
        return action

    # -- step 3: execution -------------------------------------------------
    def execute(self, action_id: str, principal: Principal) -> ActionRequest:
        action = self._require(action_id)
        self._expire_if_stale(action)

        if action.status is not ActionStatus.APPROVED:
            self.audit.record("action_execution_blocked", trace_id=action.trace_id,
                              actor=principal.id,
                              payload={"action_id": action_id, "status": action.status.value})
            raise AuthorizationError(
                f"cannot execute: action is {action.status.value}, not APPROVED")

        # Re-derive the hash now. Approval was granted for specific content; if
        # the content moved since, the approval does not carry over to it.
        if canonical_hash(action.payload) != action.payload_hash:
            action.status = ActionStatus.FAILED
            self._persist(action)
            self.audit.record("action_integrity_violation", trace_id=action.trace_id,
                              actor=principal.id, payload={"action_id": action_id})
            raise AuthorizationError("payload changed after approval; execution refused")

        spec = self.registry.get(action.tool)
        if spec is None:
            raise AuthorizationError(f"tool '{action.tool}' is no longer registered")

        try:
            result = spec.handler(action.payload)
            action.status = ActionStatus.EXECUTED
            action.execution_result = result
        except Exception as exc:  # noqa: BLE001
            action.status = ActionStatus.FAILED
            action.execution_result = {"error": f"{type(exc).__name__}: {exc}"}
        self._persist(action)
        self.audit.record("action_executed", trace_id=action.trace_id, actor=principal.id, payload={
            "action_id": action_id, "tool": action.tool, "status": action.status.value,
            "approved_by": action.approved_by, "result": action.execution_result,
        })
        return action

    # -- queries -----------------------------------------------------------
    def get(self, action_id: str) -> ActionRequest | None:
        action = self._actions.get(action_id)
        if action:
            self._expire_if_stale(action)
        return action

    def pending(self) -> list[ActionRequest]:
        for a in self._actions.values():
            self._expire_if_stale(a)
        return [a for a in self._actions.values() if a.status is ActionStatus.PENDING]

    def all(self) -> list[ActionRequest]:
        return sorted(self._actions.values(), key=lambda a: a.created_at, reverse=True)

    # -- internals ---------------------------------------------------------
    def _valid_token(self, token: str | None) -> bool:
        configured = self.settings.approver_token
        if not token or not configured or len(configured) < 32:
            return False
        return hmac.compare_digest(token, configured)

    def _deny_attempt(self, action_id: str, principal: Principal, reason: str) -> None:
        action = self._actions.get(action_id)
        self.audit.record("authorization_violation",
                          trace_id=action.trace_id if action else "unknown",
                          actor=principal.id,
                          payload={"action_id": action_id, "principal_kind": principal.kind,
                                   "reason": reason})

    def _require(self, action_id: str) -> ActionRequest:
        action = self._actions.get(action_id)
        if action is None:
            raise AuthorizationError(f"unknown action '{action_id}'")
        return action

    def _expire_if_stale(self, action: ActionRequest) -> None:
        if action.status is ActionStatus.PENDING and utcnow() > action.expires_at:
            action.status = ActionStatus.EXPIRED
            self._persist(action)
            self.audit.record("action_expired", trace_id=action.trace_id, actor="system",
                              payload={"action_id": action.action_id})

    def _persist(self, action: ActionRequest) -> None:
        self._actions[action.action_id] = action
        with self.store_path.open("a", encoding="utf-8") as fh:
            fh.write(action.model_dump_json() + "\n")

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        for line in self.store_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                action = ActionRequest.model_validate_json(line)
                self._actions[action.action_id] = action   # last write wins


def new_approver_token() -> str:
    return secrets.token_urlsafe(32)
