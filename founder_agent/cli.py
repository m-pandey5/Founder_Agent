"""Command line entry point.

    python -m founder_agent.cli demo                 # the seven capabilities, end to end
    python -m founder_agent.cli ask "<question>"
    python -m founder_agent.cli contradictions
    python -m founder_agent.cli ledger
    python -m founder_agent.cli audit <trace_id>
    python -m founder_agent.cli serve
"""

from __future__ import annotations

import argparse
import json
import sys

from .app import build_system
from .authorization import AuthorizationError, Principal

BOLD, DIM, RED, GRN, YEL, CYN, OFF = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m")


def _h(title: str) -> None:
    print(f"\n{BOLD}{'=' * 76}\n{title}\n{'=' * 76}{OFF}")


def _answer(resp) -> None:
    print(f"{BOLD}verdict:{OFF} {resp.answer.verdict}   "
          f"{DIM}plan={resp.plan.intent} provider={resp.provider} trace={resp.trace_id}{OFF}")
    for s in resp.answer.segments:
        print(f"  {CYN}[{s.claim_type.value}]{OFF} {s.text}  {DIM}{s.citations}{OFF}")
    for m in resp.answer.missing:
        print(f"  {YEL}?{OFF} {m}")
    for c in resp.answer.caveats:
        print(f"  {YEL}!{OFF} {c}")


def cmd_ask(system, args) -> int:
    resp = system.orchestrator.ask(args.question, prepare_action=args.prepare_action)
    _answer(resp)
    if resp.decision and resp.decision.found:
        g = resp.decision.governing
        print(f"\n{BOLD}governing decision:{OFF} {g.doc_id} [{g.status.value}] "
              f"{g.authority.value} {g.date}\n  {g.decision_text}")
        for n in g.notes:
            print(f"  {DIM}· {n}{OFF}")
        c = resp.decision.proposal_conflict
        if c and c.conflicts:
            print(f"\n  {RED}CONFLICT ({c.severity}):{OFF} {c.explanation}")
    for d in resp.delegations:
        print(f"\n{BOLD}delegated -> {d.agent}{OFF} ({d.status}, confidence {d.confidence}, "
              f"{d.provider}/{d.model})\n  {d.summary}")
        for f in d.findings[:4]:
            print(f"  • {f}")
    for cl in resp.contradictions:
        print(f"\n{BOLD}contradiction:{OFF} [{cl.severity}] {cl.title} {DIM}{cl.documents}{OFF}")
    if resp.pending_action:
        a = resp.pending_action
        print(f"\n{YEL}ACTION PREPARED, NOT SENT{OFF}  {a.action_id}  status={a.status.value}")
        print(f"  {a.intent}\n  payload sha256: {a.payload_hash}")
    return 0


def cmd_contradictions(system, _args) -> int:
    for cl in system.detector.cluster():
        print(f"\n{BOLD}[{cl.severity}] {cl.title}{OFF}  {DIM}{cl.documents}{OFF}")
        p = cl.primary
        print(f"  {CYN}{p.claim_a.claim_id}{OFF} {p.claim_a.text}")
        print(f"  {CYN}{p.claim_b.claim_id}{OFF} {p.claim_b.text}")
        print(f"  {BOLD}why:{OFF} {p.why}")
        print(f"  {BOLD}resolution:{OFF} {p.resolution}")
        if cl.current_position:
            print(f"  {BOLD}today:{OFF} {cl.current_position}")
        print(f"  {DIM}detector: {p.detector}; +{len(cl.supporting)} supporting pair(s){OFF}")
    return 0


def cmd_ledger(system, _args) -> int:
    for e in system.ledger.entries.values():
        print(f"{e.doc_id}  {e.status.value:11} {e.authority.value:12} {e.date}  {e.title}")
        for n in e.notes:
            print(f"   {DIM}· {n}{OFF}")
    return 0


def cmd_audit(system, args) -> int:
    print(json.dumps(system.audit.reconstruct(args.trace_id), indent=2, default=str))
    return 0


def cmd_verify(system, _args) -> int:
    print(json.dumps(system.audit.verify(), indent=2))
    return 0


def cmd_serve(_system, args) -> int:
    import uvicorn
    uvicorn.run("founder_agent.api:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_demo(system, _args) -> int:
    # The demo uses a fresh process-local credential when none was configured;
    # unlike the removed development default, it is random and never printed.
    if not system.settings.approver_token:
        from .authorization import new_approver_token
        system.settings.approver_token = new_approver_token()

    _h("2.1  KNOWLEDGE RETRIEVAL - answers carry sources; thin evidence returns UNKNOWN")
    for q in ("What signing algorithm and key custody boundary do we use?",
              "What is our Series A valuation?"):
        print(f"\n{BOLD}Q:{OFF} {q}")
        _answer(system.orchestrator.ask(q))

    _h("2.2  DECISION RETRIEVAL - governing record, status, source, rationale, conflict")
    resp = system.orchestrator.ask("Should we add KeyForge as a second custody provider?")
    g = resp.decision.governing
    print(f"governing : {g.doc_id} [{g.status.value}] {g.authority.value} {g.date}")
    print(f"source    : {g.source}")
    print(f"decision  : {g.decision_text}")
    print(f"rationale : {g.rationale}")
    for n in g.notes:
        print(f"note      : {n}")
    print(f"history   : {[(h.doc_id, h.status.value) for h in resp.decision.history]}")
    print(f"\n{RED}conflict with your proposal ({resp.decision.proposal_conflict.severity}):{OFF}")
    print(f"  {resp.decision.proposal_conflict.explanation}")

    _h("2.3  CONTRADICTION DETECTION - why they conflict, not that words differ")
    cmd_contradictions(system, None)

    _h("2.4  CLASSIFICATION - every claim keeps its epistemic label")
    from collections import Counter
    counts = Counter(c.claim_type.value for c in system.claims)
    print(dict(counts))
    for cid in ("DOC-01#c1", "DOC-03#c1", "DOC-07#c3", "DOC-11#c1", "DOC-10#c3"):
        c = next(x for x in system.claims if x.claim_id == cid)
        print(f"  {cid:12} {c.claim_type.value:11} {DIM}({c.type_reason}){OFF}")
        print(f"               {c.text[:88]}")
    print(f"\n{DIM}Note DOC-07#c3: mentions a hypothesis but is INFERENCE - reporting a "
          f"hypothesis is not holding one.{OFF}")

    _h("2.5  AGENT DELEGATION - parent delegates, subordinates return typed results")
    for d in resp.delegations:
        print(f"\n{BOLD}{d.agent}{OFF}  status={d.status} confidence={d.confidence} "
              f"provider={d.provider}/{d.model}")
        print(f"  task      : {d.task}")
        print(f"  summary   : {d.summary}")
        for f in d.findings[:3]:
            print(f"  finding   : {f[:150]}")
        for r in d.refusals:
            print(f"  {YEL}refused   : {r}{OFF}")

    _h("2.6  HUMAN AUTHORIZATION - prepared, then blocked until a human approves")
    action = system.orchestrator.ask(
        "Send Northbridge Bank our current custody position of record",
        prepare_action=True).pending_action
    print(f"prepared  : {action.action_id}  status={action.status.value}")
    print(f"intent    : {action.intent}")
    print(f"hash      : {action.payload_hash}")
    for w in action.warnings:
        print(f"{YEL}warning   : {w}{OFF}")

    print(f"\n{BOLD}attempt 1 - the agent tries to approve its own action{OFF}")
    try:
        system.gateway.approve(action.action_id,
                               Principal(kind="agent", id="founder_agent.orchestrator",
                                         token=system.settings.approver_token))
        print(f"  {RED}FAILED: agent approval accepted{OFF}")
    except AuthorizationError as exc:
        print(f"  {GRN}refused:{OFF} {exc}")

    print(f"\n{BOLD}attempt 2 - execute without approval{OFF}")
    try:
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))
        print(f"  {RED}FAILED: executed without approval{OFF}")
    except AuthorizationError as exc:
        print(f"  {GRN}refused:{OFF} {exc}")

    print(f"\n{BOLD}attempt 3 - a human approves, then the payload is tampered with{OFF}")
    system.gateway.approve(action.action_id,
                           Principal(kind="human", id="founder",
                                     token=system.settings.approver_token))
    action.payload["to"] = "attacker@evil.invalid"
    try:
        system.gateway.execute(action.action_id, Principal(kind="human", id="founder"))
        print(f"  {RED}FAILED: tampered payload executed{OFF}")
    except AuthorizationError as exc:
        print(f"  {GRN}refused:{OFF} {exc}")

    _h("2.7  AUDITABILITY - the whole interaction, reconstructed from the log")
    record = system.audit.reconstruct(resp.trace_id)
    print(f"trace {resp.trace_id}: {record['event_count']} events")
    for e in record["timeline"]:
        print(f"  #{e['seq']:<4} {e['ts'][11:19]}  {e['actor']:<28} {e['event']}")
    print(f"\nchain verification: {system.audit.verify()}")
    print(f"{DIM}Secrets are redacted before the write, so the log cannot leak them later.{OFF}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="founder_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ask"); p.add_argument("question")
    p.add_argument("--prepare-action", action="store_true"); p.set_defaults(fn=cmd_ask)
    sub.add_parser("contradictions").set_defaults(fn=cmd_contradictions)
    sub.add_parser("ledger").set_defaults(fn=cmd_ledger)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    p = sub.add_parser("audit"); p.add_argument("trace_id"); p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    return args.fn(build_system(), args)


if __name__ == "__main__":
    sys.exit(main())
