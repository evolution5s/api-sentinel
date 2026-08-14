"""Main-CEO / holding-level governance tools.

This is the layer above tools.py: tools.py is a single subsidiary's own
Sub-CEO/operative state (hypotheses, channels, approvals). holding.py is the
Main-CEO's own domain - the subsidiary registry, pivot-proposal review,
cross-subsidiary request routing, and the shared "pull principle" research
archive - kept in its own STATE_DIR/_holding/ subfolder so it never mixes
into a subsidiary's own files.

With only one subsidiary (api-sentinel) registered today, most of this is
scaffolding that becomes load-bearing once a second subsidiary exists. The
existing human approval queue (tools.request_approval / approval_queue.jsonl)
stays the single source of truth for Aufsichtsrat sign-off - this module
never adds a second approval mechanism, only reads/reuses that one.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from crewai.tools import tool

from jsonl_store import append_jsonl, locked, read_jsonl, write_jsonl
from scoring import HYPOTHESIS_OUTCOMES
from tools import EVIDENCE_STAGES, STATE_DIR as SUBSIDIARY_STATE_DIR, _instruction_echo_match

HOLDING_DIR = SUBSIDIARY_STATE_DIR / "_holding"

SUBSIDIARY_STATUSES = {"active", "dormant"}
PIVOT_DECISIONS = {"approve_in_place", "move_to_subsidiary", "spinoff_required", "rejected"}
CROSS_REQUEST_DECISIONS = {"approved", "rejected"}
REQUIRED_PIVOT_FIELDS = {
    "nature_of_change", "validating_data", "evolutionary_or_disruptive",
    "existing_business_disposition", "capability_gap_analysis",
    "new_resources_needed", "risk_assessment", "synergy_overlap",
}
REQUIRED_SUBSIDIARY_FIELDS = {"id", "name", "focus"}
IDEA_STATUSES = {"pending", "routed"}
IDEA_ROUTING_DECISIONS = {"existing_subsidiary", "new_subsidiary", "rejected"}
# What "a new subsidiary" concretely means in this system today, stated
# plainly on every subsidiary record rather than left implicit. Updated by
# the structural-rebuild addendum, section 2: data isolation is now real -
# tools.py resolves every subsidiary-scoped file under STATE_DIR/<subsidiary_
# id>/ (set_active_subsidiary), and crew.py loops crew.kickoff() once per
# active subsidiary (task_ceo/task_channel_strategy/etc. text is interpolated
# with {subsidiary_id} rather than hardcoded), so a newly-registered
# subsidiary genuinely gets its own hypotheses/channels/etc. and a real crew
# run, not bookkeeping-only. What's still NOT subsidiary-aware: the actual
# CONTENT of the task instructions (task_channel_strategy's concrete channel
# candidates, the Freqtrade/CCXT-specific framing running through task_ceo/
# task_growth) is still written for API Sentinel's specific business - a
# second subsidiary would run through the same real operative loop with
# instructions that don't match its actual business until those are made
# business-agnostic or subsidiary-specific too (a separate, larger piece of
# work, not blocking today with only one subsidiary).
NEW_SUBSIDIARY_CAPABILITY_NOTE = (
    "data_isolated_generic_tasks - this subsidiary gets its own STATE_DIR/"
    "<id>/ data partition and is picked up by crew.py's per-active-subsidiary "
    "loop automatically. NOT yet true: the task instructions themselves "
    "(channel candidates, business framing in task_ceo/task_growth/"
    "task_channel_strategy) are still written specifically for API "
    "Sentinel's business - review/rewrite those before expecting this "
    "subsidiary's cycles to produce business-appropriate output."
)

# General prerequisites every subsidiary operates under by default - the
# Main-CEO can instruct any Sub-CEO to build essentially any company, but
# every one of them has to clear these first, regardless of what it does.
# Per-subsidiary, adjustable (e.g. re-allow paid channels for one specific
# subsidiary later) but never silently - only via update_subsidiary_policies,
# which is approval-gated the same way register_subsidiary already is.
SUBSIDIARY_POLICY_DEFAULTS = {
    "paid_channels_allowed": False,
    "cold_email_allowed": False,
    "data_collection_allowed": False,
    "risk_tolerance": "low",
}


def _read(filename: str) -> list:
    return read_jsonl(HOLDING_DIR, filename)


def _write(filename: str, records: list) -> None:
    write_jsonl(HOLDING_DIR, filename, records)


def _jsonl_lock(filename: str):
    """Hold across a _read()->mutate->_write() span - see jsonl_store.locked()."""
    return locked(HOLDING_DIR, filename)


def _append(filename: str, record: dict) -> None:
    append_jsonl(HOLDING_DIR, filename, record)


def _bootstrap_default_subsidiary() -> dict:
    """api-sentinel already exists and already has real accumulated state -
    this just models the fact in the registry, it is not a new company.
    """
    return {
        "id": "api-sentinel",
        "name": "API Sentinel",
        "focus": (
            "Exchange API change monitoring for the Freqtrade/CCXT quant-bot "
            "community. Currently in the hypothesis-testing/Lean-Startup "
            "phase - not yet building the monitoring product itself."
        ),
        "status": "active",
        "state_dir": str(SUBSIDIARY_STATE_DIR / "api-sentinel"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status_history": [],
        "policies": dict(SUBSIDIARY_POLICY_DEFAULTS),
        "notes": (
            "Pre-existing subsidiary, auto-registered when the Main-CEO/"
            "Sub-CEO holding layer was added."
        ),
    }


def _all_subsidiaries() -> list:
    with _jsonl_lock("subsidiaries.jsonl"):
        subs = _read("subsidiaries.jsonl")
        if not subs:
            subs = [_bootstrap_default_subsidiary()]
            _write("subsidiaries.jsonl", subs)
    return subs


# --------------------------------------------------------------------------
# Subsidiary registry (Main-CEO)
# --------------------------------------------------------------------------

@tool("read_subsidiaries")
def read_subsidiaries(status: str = "") -> str:
    """Return the holding's subsidiary registry as JSON: id, name, focus,
    status (active/dormant), state_dir, status_history. Auto-registers
    api-sentinel on first call if the registry is still empty - it already
    exists, this just models it. Pass status to filter, or "" for all.
    """
    subs = _all_subsidiaries()
    if status:
        subs = [s for s in subs if s.get("status") == status]
    return json.dumps(subs, ensure_ascii=False)


@tool("register_subsidiary")
def register_subsidiary(subsidiary: str, approved_request_id: str) -> str:
    """Register a new subsidiary (spin-off) in the holding's registry. This
    creates the metadata entry AND a real, isolated data partition
    (STATE_DIR/<id>/ - see NEW_SUBSIDIARY_CAPABILITY_NOTE on the record for
    what's still not automatic, e.g. business-specific task content). It
    does NOT provision a new Railway service - one service, one crew.py
    process, one cron handles every active subsidiary via crew.py's per-
    subsidiary loop. Requires approved_request_id pointing at an approved
    entry in the existing human approval queue - reuses request_approval,
    no separate gate. Never fabricate an approval id; if none exists yet,
    file request_approval first and wait.
    `subsidiary` is a JSON string with at least id, name, focus.
    """
    try:
        patch = json.loads(subsidiary)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    missing = REQUIRED_SUBSIDIARY_FIELDS - patch.keys()
    if missing:
        return json.dumps({"error": f"missing required fields: {sorted(missing)}"})

    approvals = read_jsonl(SUBSIDIARY_STATE_DIR, "approval_queue.jsonl")
    approval = next((a for a in approvals if a.get("id") == approved_request_id), None)
    if not approval or approval.get("status") != "approved":
        return json.dumps({
            "error": "register_subsidiary requires approved_request_id pointing at an "
                     "approved entry in the human approval queue - file request_approval "
                     "first (instantiating a new Sub-CEO always needs the Aufsichtsrat's Go)"
        })

    subs = _all_subsidiaries()
    if any(s.get("id") == patch["id"] for s in subs):
        return json.dumps({"error": f"subsidiary '{patch['id']}' is already registered"})

    # Policies default to the general prerequisites (no paid channels, no
    # cold email, no data collection, low risk tolerance) unless the patch
    # explicitly overrides one - a new subsidiary never silently inherits a
    # looser policy than the baseline just because nobody mentioned it.
    policies = {**SUBSIDIARY_POLICY_DEFAULTS, **(patch.get("policies") or {})}

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "state_dir": str(SUBSIDIARY_STATE_DIR / patch["id"]),
        "operative_capability": NEW_SUBSIDIARY_CAPABILITY_NOTE,
        "status_history": [{
            "at": datetime.now(timezone.utc).isoformat(), "to": "active",
            "reason": f"spin-off approved via {approved_request_id}",
        }],
        **patch,
        "policies": policies,
    }
    subs.append(record)
    _write("subsidiaries.jsonl", subs)
    return json.dumps({"ok": True, "id": patch["id"], "policies": policies})


@tool("read_subsidiary_policies")
def read_subsidiary_policies(subsidiary_id: str) -> str:
    """Read the general prerequisites a subsidiary currently operates
    under (paid_channels_allowed, cold_email_allowed, data_collection_
    allowed, risk_tolerance). Every subsidiary has these, defaulting to the
    conservative baseline (no paid channels, no cold email, no data
    collection, low risk) unless a human explicitly loosened one via
    update_subsidiary_policies. A Sub-CEO should check this before
    generating channel candidates or hypotheses that would touch any of
    these - e.g. never brainstorm paid-ads channels while
    paid_channels_allowed is false.
    """
    subs = _all_subsidiaries()
    sub = next((s for s in subs if s.get("id") == subsidiary_id), None)
    if sub is None:
        return json.dumps({"error": f"no subsidiary with id '{subsidiary_id}'"})
    return json.dumps(sub.get("policies") or dict(SUBSIDIARY_POLICY_DEFAULTS), ensure_ascii=False)


@tool("update_subsidiary_policies")
def update_subsidiary_policies(subsidiary_id: str, policies_patch: str, approved_request_id: str, reasoning: str) -> str:
    """Main-CEO adjusts a subsidiary's general prerequisites - e.g.
    re-allowing paid channels for one specific subsidiary. Requires
    approved_request_id pointing at an approved entry in the human approval
    queue, same as register_subsidiary - loosening a prerequisite like "no
    paid ads" or "no cold email" is exactly the kind of decision that needs
    the Aufsichtsrat's sign-off, never something a Sub-CEO or the Main-CEO
    grants itself. `policies_patch` is a JSON object with only the keys
    being changed, e.g. '{"paid_channels_allowed": true}'.
    """
    try:
        patch = json.loads(policies_patch)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    unknown = set(patch.keys()) - set(SUBSIDIARY_POLICY_DEFAULTS.keys())
    if unknown:
        return json.dumps({"error": f"unknown policy keys: {sorted(unknown)}, must be a subset of {sorted(SUBSIDIARY_POLICY_DEFAULTS.keys())}"})

    approvals = read_jsonl(SUBSIDIARY_STATE_DIR, "approval_queue.jsonl")
    approval = next((a for a in approvals if a.get("id") == approved_request_id), None)
    if not approval or approval.get("status") != "approved":
        return json.dumps({
            "error": "update_subsidiary_policies requires approved_request_id pointing at an "
                     "approved entry in the human approval queue - file request_approval first"
        })

    subs = _all_subsidiaries()
    idx = next((i for i, s in enumerate(subs) if s.get("id") == subsidiary_id), None)
    if idx is None:
        return json.dumps({"error": f"no subsidiary with id '{subsidiary_id}'"})

    current = {**SUBSIDIARY_POLICY_DEFAULTS, **(subs[idx].get("policies") or {})}
    subs[idx]["policies"] = {**current, **patch}
    subs[idx].setdefault("policy_history", []).append({
        "at": datetime.now(timezone.utc).isoformat(), "changed": patch,
        "approved_request_id": approved_request_id, "reasoning": reasoning,
    })
    _write("subsidiaries.jsonl", subs)
    return json.dumps({"ok": True, "id": subsidiary_id, "policies": subs[idx]["policies"]})


@tool("set_subsidiary_status")
def set_subsidiary_status(subsidiary_id: str, status: str, reason: str) -> str:
    """Set a subsidiary to 'active' or 'dormant'. Dormant subsidiaries are
    never deleted - they keep their accumulated knowledge/state and can be
    reactivated later; this only marks that they're not currently being
    worked on. Always requires a non-empty reason (audit trail, same
    pattern as channel status changes).
    """
    if status not in SUBSIDIARY_STATUSES:
        return json.dumps({"error": f"invalid status '{status}', must be one of {sorted(SUBSIDIARY_STATUSES)}"})
    if not reason.strip():
        return json.dumps({"error": "status change requires a non-empty reason"})

    subs = _all_subsidiaries()
    idx = next((i for i, s in enumerate(subs) if s.get("id") == subsidiary_id), None)
    if idx is None:
        return json.dumps({"error": f"no subsidiary with id '{subsidiary_id}'"})

    subs[idx]["status"] = status
    subs[idx].setdefault("status_history", []).append({
        "at": datetime.now(timezone.utc).isoformat(), "to": status, "reason": reason,
    })
    _write("subsidiaries.jsonl", subs)
    return json.dumps({"ok": True, "id": subsidiary_id, "status": status})


# --------------------------------------------------------------------------
# Idea intake and Main-CEO routing (audit addendum, section 4). Any agent -
# Main-CEO, a Sub-CEO, or in principle Growth surfacing a market gap from
# real community engagement - can propose an idea here. It sits pending
# until the Main-CEO reviews it (task_main_ceo_review step 0) and routes it:
# into an existing active subsidiary's strategic direction, toward a new
# subsidiary (still gated by the existing register_subsidiary approval
# requirement - route_idea itself never creates one), or rejected outright.
# Deliberately a flat two-step flow (propose -> route) rather than auto-
# chaining into set_strategic_direction/register_subsidiary itself, same
# separation as file_pivot_proposal/decide_pivot_proposal above: routing is
# a decision on record, acting on it is still the Main-CEO's own next tool
# call, with its own reasoning.
# --------------------------------------------------------------------------

@tool("propose_idea")
def propose_idea(summary: str, source: str, reasoning: str) -> str:
    """Propose an idea (a market gap / value-creation opportunity) into the
    holding-level intake queue for the Main-CEO to evaluate and route. Any
    agent can call this - a Sub-CEO noticing something adjacent to its own
    focus, Growth surfacing a real pattern from community engagement, or the
    Main-CEO itself. `source` should identify where this came from (e.g. a
    subsidiary_id, or "main_ceo", or a short free-text origin if neither
    applies). This only queues the idea - it does not evaluate or act on it;
    see route_idea for that.
    """
    if not summary.strip():
        return json.dumps({"error": "summary must not be empty"})
    record = {
        "id": f"idea_{uuid.uuid4().hex[:8]}",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "source": source,
        "reasoning": reasoning,
        "status": "pending",
        "routing_decision": None,
        "routing_reasoning": None,
        "target_subsidiary_id": None,
    }
    _append("ideas.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_ideas")
def read_ideas(status: str = "") -> str:
    """Return proposed ideas as JSON. Pass status="pending" for what's
    actually waiting on Main-CEO review, "routed" for already-decided ones,
    or "" for all.
    """
    ideas = _read("ideas.jsonl")
    if status:
        ideas = [i for i in ideas if i.get("status") == status]
    return json.dumps(ideas, ensure_ascii=False)


@tool("route_idea")
def route_idea(idea_id: str, decision: str, reasoning: str, target_subsidiary_id: str = "") -> str:
    """Main-CEO routes a pending idea. decision must be one of:
    existing_subsidiary (fits an already-active subsidiary's portfolio -
    requires target_subsidiary_id; follow up yourself with
    set_strategic_direction on that subsidiary to actually act on it, this
    tool only records the routing decision), new_subsidiary (needs its own
    subsidiary - still requires a separate request_approval +
    register_subsidiary afterward, exactly like any other spin-off; read
    register_subsidiary's own docstring before promising this idea an
    operative capability it doesn't have yet), or rejected (say why in
    reasoning). Never re-routes an already-routed idea.
    """
    if decision not in IDEA_ROUTING_DECISIONS:
        return json.dumps({"error": f"invalid decision '{decision}', must be one of {sorted(IDEA_ROUTING_DECISIONS)}"})
    if decision == "existing_subsidiary" and not target_subsidiary_id.strip():
        return json.dumps({"error": "decision='existing_subsidiary' requires target_subsidiary_id"})
    if decision == "existing_subsidiary":
        subs = _all_subsidiaries()
        if not any(s.get("id") == target_subsidiary_id for s in subs):
            return json.dumps({"error": f"no subsidiary with id '{target_subsidiary_id}'"})

    # Main-CEO routes several pending ideas per turn, and crewai can run
    # those calls concurrently - see tools.write_backlog_candidate's
    # comment on why the whole read->mutate->write span must be locked.
    with _jsonl_lock("ideas.jsonl"):
        ideas = _read("ideas.jsonl")
        idx = next((i for i, rec in enumerate(ideas) if rec.get("id") == idea_id), None)
        if idx is None:
            return json.dumps({"error": f"no idea with id '{idea_id}'"})
        if ideas[idx].get("status") == "routed":
            return json.dumps({
                "error": f"'{idea_id}' was already routed ({ideas[idx].get('routing_decision')}), not re-routing"
            })

        ideas[idx]["status"] = "routed"
        ideas[idx]["routing_decision"] = decision
        ideas[idx]["routing_reasoning"] = reasoning
        ideas[idx]["target_subsidiary_id"] = target_subsidiary_id or None
        ideas[idx]["routed_at"] = datetime.now(timezone.utc).isoformat()
        _write("ideas.jsonl", ideas)
    return json.dumps({"ok": True, "id": idea_id, "decision": decision})


# --------------------------------------------------------------------------
# Pivot proposal review (Sub-CEO files, Main-CEO decides)
# --------------------------------------------------------------------------

@tool("file_pivot_proposal")
def file_pivot_proposal(subsidiary_id: str, proposal: str) -> str:
    """File a structured PIVOT & STRATEGY PROPOSAL for the Main-CEO to
    review. Sub-CEOs never decide a fundamental strategy change themselves
    or escalate it straight to the human board - pivots always go through
    the Main-CEO first.
    `proposal` must be a JSON string with these required template fields:
    nature_of_change, validating_data, evolutionary_or_disruptive,
    existing_business_disposition, capability_gap_analysis,
    new_resources_needed, risk_assessment, synergy_overlap.
    """
    try:
        patch = json.loads(proposal)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    missing = REQUIRED_PIVOT_FIELDS - patch.keys()
    if missing:
        return json.dumps({"error": f"proposal missing required template fields: {sorted(missing)}"})

    record = {
        "id": f"pivot_{uuid.uuid4().hex[:8]}",
        "subsidiary_id": subsidiary_id,
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "decision": None,
        "decision_reasoning": None,
        **patch,
    }
    _append("pivot_proposals.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_pivot_proposals")
def read_pivot_proposals(status: str = "") -> str:
    """Return filed pivot proposals as JSON. Pass status="pending" or
    status="decided" to filter, or "" for all.
    """
    proposals = _read("pivot_proposals.jsonl")
    if status:
        proposals = [p for p in proposals if p.get("status") == status]
    return json.dumps(proposals, ensure_ascii=False)


@tool("decide_pivot_proposal")
def decide_pivot_proposal(proposal_id: str, decision: str, reasoning: str) -> str:
    """Decide a pending pivot proposal. decision must be one of:
    approve_in_place (the pivot happens within the same subsidiary),
    move_to_subsidiary (fits an existing different subsidiary's portfolio
    better), spinoff_required (needs a brand-new subsidiary - still needs a
    separate approved request_approval before register_subsidiary can
    create it), or rejected. Never re-decides an already-decided proposal.
    """
    if decision not in PIVOT_DECISIONS:
        return json.dumps({"error": f"invalid decision '{decision}', must be one of {sorted(PIVOT_DECISIONS)}"})

    # See route_idea's comment: batch-decided per turn, needs the lock
    # held across the whole read->mutate->write span.
    with _jsonl_lock("pivot_proposals.jsonl"):
        proposals = _read("pivot_proposals.jsonl")
        idx = next((i for i, p in enumerate(proposals) if p.get("id") == proposal_id), None)
        if idx is None:
            return json.dumps({"error": f"no pivot proposal with id '{proposal_id}'"})
        if proposals[idx].get("status") == "decided":
            return json.dumps({
                "error": f"'{proposal_id}' was already decided ({proposals[idx].get('decision')}), not re-deciding"
            })

        proposals[idx]["status"] = "decided"
        proposals[idx]["decision"] = decision
        proposals[idx]["decision_reasoning"] = reasoning
        proposals[idx]["decided_at"] = datetime.now(timezone.utc).isoformat()
        _write("pivot_proposals.jsonl", proposals)
    return json.dumps({"ok": True, "id": proposal_id, "decision": decision})


# --------------------------------------------------------------------------
# Evidence-stage skip review (structural-rebuild addendum, section 4).
# write_hypothesis rejects a hypothesis crossing into landing_page/build (or
# community_engagement) without artifact-backed history through the earlier
# stages - unless a stage-skip request for that exact hypothesis_id/
# target_stage is on record here with status='approved'. Mirrors file_
# pivot_proposal/decide_pivot_proposal's Sub-CEO-files/Main-CEO-decides
# shape exactly, kept separate from pivot_proposals.jsonl since a stage
# skip is a narrower, differently-shaped decision (which stage, why skip
# it) than a full strategy pivot (8 required template fields) - forcing
# stage-skip reasoning through the pivot template would be an awkward fit
# for what is usually a much smaller, tactical call.
# --------------------------------------------------------------------------

STAGE_SKIP_DECISIONS = {"approved", "rejected"}


@tool("file_stage_skip_request")
def file_stage_skip_request(hypothesis_id: str, subsidiary_id: str, target_stage: str, reasoning: str) -> str:
    """Sub-CEO asks the Main-CEO to approve skipping straight to
    target_stage (landing_page/build, or community_engagement) for
    hypothesis_id without the artifact-backed research/community_engagement
    history write_hypothesis normally requires to get there. Only file this
    when skipping genuinely applies (e.g. research truly isn't relevant to
    this specific question) - not as a routine way around the gate; the
    Main-CEO is expected to actually push back when it isn't warranted.
    reasoning must trace to something real about this specific hypothesis,
    not general system knowledge or reused instruction wording (rejected if
    it echoes known template phrasing, same check as build_cost_reasoning).
    """
    if target_stage not in EVIDENCE_STAGES:
        return json.dumps({"error": f"invalid target_stage '{target_stage}', must be one of {EVIDENCE_STAGES}"})
    if not reasoning.strip():
        return json.dumps({"error": "reasoning must not be empty"})
    echoed = _instruction_echo_match(reasoning)
    if echoed:
        return json.dumps({
            "error": f"reasoning echoes known instruction/incident template language ('{echoed}') rather "
                     "than something specific to this hypothesis - describe the actual reason, don't reuse "
                     "example wording"
        })

    record = {
        "id": f"skip_{uuid.uuid4().hex[:8]}",
        "hypothesis_id": hypothesis_id,
        "subsidiary_id": subsidiary_id,
        "target_stage": target_stage,
        "reasoning": reasoning,
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "decision_reasoning": None,
        "decided_at": None,
    }
    _append("stage_skip_requests.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_stage_skip_requests")
def read_stage_skip_requests(status: str = "") -> str:
    """Return filed stage-skip requests as JSON. Pass status="pending" for
    what's actually waiting on Main-CEO review, "approved"/"rejected" for
    already-decided ones, or "" for all.
    """
    requests = _read("stage_skip_requests.jsonl")
    if status:
        requests = [r for r in requests if r.get("status") == status]
    return json.dumps(requests, ensure_ascii=False)


@tool("decide_stage_skip_request")
def decide_stage_skip_request(request_id: str, decision: str, reasoning: str) -> str:
    """Main-CEO decides a pending stage-skip request. decision must be
    'approved' (the skip is genuinely warranted - e.g. research truly
    doesn't apply to this question) or 'rejected' (send it back to do the
    earlier stages properly). Never re-decides an already-decided request -
    file a new one if circumstances genuinely change.
    """
    if decision not in STAGE_SKIP_DECISIONS:
        return json.dumps({"error": f"invalid decision '{decision}', must be one of {sorted(STAGE_SKIP_DECISIONS)}"})

    # See route_idea's comment: batch-decided per turn, needs the lock
    # held across the whole read->mutate->write span.
    with _jsonl_lock("stage_skip_requests.jsonl"):
        requests = _read("stage_skip_requests.jsonl")
        idx = next((i for i, r in enumerate(requests) if r.get("id") == request_id), None)
        if idx is None:
            return json.dumps({"error": f"no stage-skip request with id '{request_id}'"})
        if requests[idx].get("status") != "pending":
            return json.dumps({
                "error": f"'{request_id}' is already '{requests[idx].get('status')}', not re-deciding"
            })

        requests[idx]["status"] = decision
        requests[idx]["decision_reasoning"] = reasoning
        requests[idx]["decided_at"] = datetime.now(timezone.utc).isoformat()
        _write("stage_skip_requests.jsonl", requests)
    return json.dumps({"ok": True, "id": request_id, "decision": decision})


# --------------------------------------------------------------------------
# Cross-subsidiary requests (Sub-CEOs never talk to each other directly)
# --------------------------------------------------------------------------

@tool("file_cross_subsidiary_request")
def file_cross_subsidiary_request(from_subsidiary_id: str, to_subsidiary_id: str, request: str, reasoning: str) -> str:
    """Ask the Main-CEO to fetch help/expertise/data from another
    subsidiary - Sub-CEOs never contact each other directly. With only one
    subsidiary registered today this will usually have nowhere to route to;
    file it anyway so the request is on record, and expect it back rejected
    or unfulfillable until a second subsidiary exists.
    """
    record = {
        "id": f"xreq_{uuid.uuid4().hex[:8]}",
        "from_subsidiary_id": from_subsidiary_id,
        "to_subsidiary_id": to_subsidiary_id,
        "request": request,
        "reasoning": reasoning,
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "result": None,
    }
    _append("cross_subsidiary_requests.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_cross_subsidiary_requests")
def read_cross_subsidiary_requests(status: str = "") -> str:
    """Return cross-subsidiary requests as JSON. Pass status="pending",
    "approved", or "rejected" to filter, or "" for all.
    """
    reqs = _read("cross_subsidiary_requests.jsonl")
    if status:
        reqs = [r for r in reqs if r.get("status") == status]
    return json.dumps(reqs, ensure_ascii=False)


@tool("resolve_cross_subsidiary_request")
def resolve_cross_subsidiary_request(request_id: str, decision: str, result: str = "", reasoning: str = "") -> str:
    """Approve or reject a pending cross-subsidiary request. decision must
    be 'approved' or 'rejected'. If approved and the target subsidiary is
    actually reachable, put whatever was retrieved in `result`; if not
    (e.g. only one subsidiary exists today), approving is still valid but
    `result` must say plainly that nothing could actually be fetched yet -
    never fabricate a result. Never re-resolves an already-resolved request.
    """
    if decision not in CROSS_REQUEST_DECISIONS:
        return json.dumps({"error": f"decision must be one of {sorted(CROSS_REQUEST_DECISIONS)}"})

    # See route_idea's comment: batch-resolved per turn, needs the lock
    # held across the whole read->mutate->write span.
    with _jsonl_lock("cross_subsidiary_requests.jsonl"):
        reqs = _read("cross_subsidiary_requests.jsonl")
        idx = next((i for i, r in enumerate(reqs) if r.get("id") == request_id), None)
        if idx is None:
            return json.dumps({"error": f"no cross-subsidiary request with id '{request_id}'"})
        if reqs[idx].get("status") != "pending":
            return json.dumps({"error": f"'{request_id}' is already '{reqs[idx].get('status')}', not touching it"})

        reqs[idx]["status"] = decision
        reqs[idx]["result"] = result
        reqs[idx]["reasoning"] = reasoning
        reqs[idx]["resolved_at"] = datetime.now(timezone.utc).isoformat()
        _write("cross_subsidiary_requests.jsonl", reqs)
    return json.dumps({"ok": True, "id": request_id, "status": decision})


# --------------------------------------------------------------------------
# Research archive (pull principle - not automatically in any agent's
# context, queried on demand by Sub-CEOs and the Main-CEO alike)
# --------------------------------------------------------------------------

_ARCHIVE_TEXT_FIELDS = ("statement", "name", "notes", "category", "focus", "nature_of_change")


def _searchable_text(record: dict) -> str:
    parts = [record[key] for key in _ARCHIVE_TEXT_FIELDS if isinstance(record.get(key), str)]
    return " ".join(parts).lower()


@tool("search_research_archive")
def search_research_archive(query: str, subsidiary_id: str = "") -> str:
    """Search across all registered subsidiaries' accumulated knowledge
    (hypotheses, channels, pivot proposals) for a keyword/phrase - the
    pull-principle research archive. Not automatically in context; call
    this when historical data or a framework from another subsidiary
    (including a dormant one) is actually needed. Case-insensitive
    substring match across each record's text fields. Pass subsidiary_id to
    scope to one subsidiary, or "" to search every registered one.
    """
    query_lower = query.strip().lower()
    if not query_lower:
        return json.dumps({"error": "query must not be empty"})

    subs = _all_subsidiaries()
    if subsidiary_id:
        subs = [s for s in subs if s.get("id") == subsidiary_id]

    results = []
    for sub in subs:
        state_dir_value = sub.get("state_dir")
        if state_dir_value:
            sub_dir = Path(state_dir_value)
            for filename in ("hypotheses.jsonl", "channels.jsonl"):
                for record in read_jsonl(sub_dir, filename):
                    if query_lower in _searchable_text(record):
                        results.append({"subsidiary_id": sub.get("id"), "source_file": filename, "record": record})
        for record in _read("pivot_proposals.jsonl"):
            if record.get("subsidiary_id") == sub.get("id") and query_lower in _searchable_text(record):
                results.append({"subsidiary_id": sub.get("id"), "source_file": "pivot_proposals.jsonl", "record": record})

    return json.dumps(
        {"query": query, "matches": results[:20], "total_matches": len(results)},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------
# Subsidiary trajectory (revenue-focus addendum, point 1b) - a lightweight,
# recurring HEALTH CHECK on whether a subsidiary is actually making progress
# (toward a validated 'build' or a clear kill) rather than spinning in
# place, independent of whatever is or isn't in the status-report/pivot-
# proposal queues this cycle. This is not a revenue tracker: a resolved
# outcome landing on 'build' only means a hypothesis cleared its own
# break-even bar, not that revenue is itself the thing being optimized for
# - see ceo_agent's Goal/backstory (crew.py) for that framing. Deliberately
# NOT a second escalation mechanism next to check_escalation (which watches
# one hypothesis lineage's rolling score right after an evaluation, from
# the Sub-CEO's side): this is a subsidiary-wide count across every
# resolved hypothesis, read by the Main-CEO every cycle regardless of what
# was escalated, and it files nothing and persists no record of its own -
# the counts just inform the Main-CEO's own cycle report.
# --------------------------------------------------------------------------

STALL_RESOLVED_THRESHOLD = 5
# Structural-rebuild addendum, section 3: possible_stall on its own was just
# a note in the Main-CEO's own report - easy to read past forever, nothing
# changed no matter how many cycles in a row it fired. This threshold is
# consecutive CYCLES (not hypotheses), since possible_stall itself is already
# gated on STALL_RESOLVED_THRESHOLD real resolved hypotheses - the underlying
# resolved-count rarely changes within a single 2h cycle (a hypothesis's own
# duration_days is realistically days, not hours), so a low cycle-count
# threshold would mostly just measure "how long since the flag first
# appeared" rather than confirm a genuinely persistent pattern. 6 consecutive
# flagged cycles (~12h at the current 2h cadence) is long enough that this
# isn't reacting to one glance or a single boundary case, short enough that a
# real pattern can't hide for days before a human sees it flagged as
# something that actually needs attention, not just noted once and repeated
# identically forever.
STAGNATION_ESCALATION_THRESHOLD = 6


@tool("assess_subsidiary_trajectory")
def assess_subsidiary_trajectory(subsidiary_id: str) -> str:
    """Deterministic health check on whether a subsidiary is actually
    making progress - counts of each outcome (build/test_further/pivot/
    bury) across every hypothesis this subsidiary has ever written, not
    just the ones flagged in a status report or pivot proposal this cycle.
    Call this every cycle regardless of whether anything else needs
    attention; it's cheap (reads one JSONL file) and doesn't depend on the
    Sub-CEO having escalated anything.

    possible_stall=true means at least STALL_RESOLVED_THRESHOLD hypotheses
    have reached a resolved outcome (build/pivot/bury - test_further is a
    continuation, not counted as resolved) and NONE of them was 'build' -
    a real signal worth saying explicitly in your own report, even without
    a formal Sub-CEO escalation. This only catches the "zero builds"
    pattern mechanically; also look at the raw outcome_counts yourself for
    the related pattern this can't compute alone - repeated inconclusive
    pivot/test_further cycles covering the same ground without ever
    reaching a real resolution is just as much a sign of spinning in place.
    Not itself an escalation trigger for a pivot decision and files no pivot
    of its own - weigh it in your own judgment alongside everything else
    this cycle, and never treat a 'build' outcome here as proof of real
    progress on its own if the underlying hypothesis never actually
    validated a genuine user problem.

    This call itself DOES persist one thing now (section 3): a consecutive-
    cycle counter for possible_stall on the subsidiary record, so the
    pattern can't silently repeat forever without ever becoming visible.
    Once it's been true for STAGNATION_ESCALATION_THRESHOLD consecutive
    calls, stagnation_escalated becomes true on the subsidiary record and
    stays there - surfaced in the Telegram report's "Fuer den Aufsichtsrat"
    section every cycle - until a human explicitly acknowledges it via
    Telegram ("stagnation_ack: <subsidiary_id>") or a real 'build' clears
    possible_stall on its own. This is visibility, not autonomous action -
    it never triggers a pivot or any strategic decision by itself, that
    still needs the board, same as any other Tier 1/2 boundary in this
    system.
    """
    # Called once per active subsidiary per cycle - today that's a single
    # call, but the race is architecturally identical to route_idea's once
    # a second subsidiary exists, so lock it now rather than waiting for
    # that to bite. _all_subsidiaries() reacquires the same (reentrant)
    # lock internally, which is fine - see jsonl_store.locked().
    with _jsonl_lock("subsidiaries.jsonl"):
        subs = _all_subsidiaries()
        idx = next((i for i, s in enumerate(subs) if s.get("id") == subsidiary_id), None)
        if idx is None:
            return json.dumps({"error": f"no subsidiary with id '{subsidiary_id}'"})
        sub = subs[idx]

        state_dir_value = sub.get("state_dir")
        if not state_dir_value:
            return json.dumps({
                "subsidiary_id": subsidiary_id, "outcome_counts": {}, "resolved_count": 0,
                "possible_stall": False, "reason": "no state_dir on record yet - nothing to assess",
            })

        outcome_counts = {"build": 0, "test_further": 0, "pivot": 0, "bury": 0}
        for h in read_jsonl(Path(state_dir_value), "hypotheses.jsonl"):
            outcome = h.get("outcome")
            if outcome in outcome_counts:
                outcome_counts[outcome] += 1

        resolved_count = outcome_counts["build"] + outcome_counts["pivot"] + outcome_counts["bury"]
        build_count = outcome_counts["build"]
        possible_stall = resolved_count >= STALL_RESOLVED_THRESHOLD and build_count == 0

        if possible_stall:
            sub["consecutive_stall_cycles"] = sub.get("consecutive_stall_cycles", 0) + 1
            if sub["consecutive_stall_cycles"] >= STAGNATION_ESCALATION_THRESHOLD and not sub.get("stagnation_escalated"):
                sub["stagnation_escalated"] = True
                sub["stagnation_escalated_at"] = datetime.now(timezone.utc).isoformat()
        else:
            # A real build (or simply not stalled anymore) clears the pattern
            # that caused the escalation - the underlying problem resolved
            # itself, no human acknowledgment needed to clear it in that case.
            sub["consecutive_stall_cycles"] = 0
            sub["stagnation_escalated"] = False
            sub["stagnation_escalated_at"] = None
        subs[idx] = sub
        _write("subsidiaries.jsonl", subs)

    return json.dumps({
        "subsidiary_id": subsidiary_id,
        "outcome_counts": outcome_counts,
        "resolved_count": resolved_count,
        "possible_stall": possible_stall,
        "consecutive_stall_cycles": sub["consecutive_stall_cycles"],
        "stagnation_escalated": sub.get("stagnation_escalated", False),
        "reason": (
            f"{resolved_count} hypotheses resolved (build/pivot/bury), 0 reached 'build' yet"
            if possible_stall else
            f"{resolved_count} resolved so far, {build_count} reached 'build'"
        ),
    })

# --------------------------------------------------------------------------
# FIX.md mechanism (FIX.md/Kaizen/payment-propensity addendum, Part 1).
# Cheap, mechanical, no-LLM checks that catch a subsidiary quietly stuck or
# repeatedly failing the same way - assess_subsidiary_trajectory above only
# catches "resolved hypotheses with zero builds"; these catch a broader set
# of real patterns (dead cycles, a recurring crash, a channel-fit failure,
# a hypothesis stuck past its own duration cap, non-converging pivots, a
# stale approval queue). Every check here is a small, pure function - no
# LLM call, testable in isolation. Only when run_fix_checks reports a fire
# does crew.py's escalated call (this module deliberately has no LLM/model
# dependency of its own - that lives in crew.py alongside the other LLM
# instances) write the result to FIX.md via append_fix_md/record_fix_entry
# below. This module never applies a fix itself - see append_fix_md's
# docstring for the guardrail.
# --------------------------------------------------------------------------

DEFAULT_PROPOSED_FIX_THRESHOLDS = {
    "status": "proposed",
    "values": {
        "zero_state_streak_cycles": 3,
        "malformed_tool_calls_cycles": 3,
        "channel_bury_streak": 3,
        "repeated_pivot_streak": 2,
        "stale_approval_hours": 48,
    },
    "note": (
        "Starting thresholds for FIX.md's mechanical trigger checks. Unlike "
        "duration caps, these gate a diagnostic write only (never a block), "
        "so they're already active against these defaults - 'proposed' means "
        "'confirm or adjust', not 'inert until confirmed'. Adjust via Telegram "
        "('fix_thresholds: confirm', or 'fix_thresholds: <zero_state_streak_cycles> "
        "<malformed_tool_calls_cycles> <channel_bury_streak> <repeated_pivot_streak> "
        "<stale_approval_hours>')."
    ),
}


def read_fix_thresholds() -> dict:
    """Holding-level, not per-subsidiary - FIX.md's diagnostic sensitivity is
    a system-wide dial. A single confirmed override record in
    fix_thresholds.jsonl replaces the proposed defaults wholesale once set
    via Telegram (tools.py's _apply_telegram_commands).
    """
    records = _read("fix_thresholds.jsonl")
    return records[0] if records else dict(DEFAULT_PROPOSED_FIX_THRESHOLDS)


def write_fix_thresholds(record: dict) -> None:
    """Public wrapper (tools.py's Telegram command handlers call this
    directly via a local `import holding`, the same locally-scoped-import
    pattern already used there for `approve`, to avoid a circular import at
    module-load time) - replaces the whole fix_thresholds.jsonl record.
    """
    _write("fix_thresholds.jsonl", [record])


def _subsidiary_state_dir(sub: dict, subsidiary_id: str) -> Path:
    state_dir_value = sub.get("state_dir") if sub else None
    return Path(state_dir_value) if state_dir_value else SUBSIDIARY_STATE_DIR / subsidiary_id


def _get_subsidiary(subsidiary_id: str):
    subs = _all_subsidiaries()
    idx = next((i for i, s in enumerate(subs) if s.get("id") == subsidiary_id), None)
    return subs, idx


# --------------------------------------------------------------------------
# Self-assessment (self-development addendum, Part F item 1). Distinct from
# the routine per-cycle business report (necessarily narrow - one cycle's
# activity) and from Kaizen (specific improvement actions): a periodic,
# honest retrospective on real cumulative progress since the last reset -
# what's actually been learned that's durable (reusable in the knowledge
# base) versus what turned out to be a dead end, and a plain statement of
# where the subsidiary genuinely stands relative to the ultimate goal (a
# validated, monetizable business). Deterministic and mechanical, same
# discipline as assess_subsidiary_trajectory just above - counts and real
# records, never a model's own narrative substituting for the numbers.
# Run on an adaptive cadence (Part D, crew.py wires {self_assessment_due}
# the same way as {kaizen_due}), not every cycle.
#
# "Since the last reset" needs no separate bookkeeping: a wipe
# (execute_confirmed_wipe) clears hypotheses.jsonl/knowledge_base.jsonl/
# usage_history.jsonl (the last one added specifically for this, see
# tools._WIPE_SUBSIDIARY_JSONL_FILES) together, so the earliest surviving
# record IS the reset boundary - a subsidiary that's never been wiped
# simply has "since" reaching back to its own real beginning.
# --------------------------------------------------------------------------

@tool("build_self_assessment")
def build_self_assessment(subsidiary_id: str) -> str:
    """Periodic honest retrospective - real cumulative progress since the
    last state wipe (or since this subsidiary began, if never wiped): every
    hypothesis tried and its outcome, tokens/cost actually spent, what's
    been durably learned (knowledge_base.jsonl - reusable takeaways) versus
    what was a dead end, and how many genuinely scored backlog candidates
    exist right now. Never fabricates a verdict - every field traces to a
    real record. Adaptively paced (Part D) - call this when {self_
    assessment_due} says yes, not every cycle.
    """
    subs, idx = _get_subsidiary(subsidiary_id)
    state_dir = _subsidiary_state_dir(subs[idx] if idx is not None else None, subsidiary_id)

    hyps = read_jsonl(state_dir, "hypotheses.jsonl")
    outcome_counts = {"build": 0, "test_further": 0, "pivot": 0, "bury": 0}
    still_open = 0
    for h in hyps:
        outcome = h.get("outcome")
        if outcome in outcome_counts:
            outcome_counts[outcome] += 1
        else:
            still_open += 1
    resolved_count = outcome_counts["build"] + outcome_counts["pivot"] + outcome_counts["bury"]

    usage = read_jsonl(state_dir, "usage_history.jsonl")
    total_tokens = sum(u.get("total_tokens") or 0 for u in usage)
    total_cost_usd = sum(u.get("cost_usd") or 0 for u in usage if u.get("cost_usd") is not None)

    knowledge = read_jsonl(state_dir, "knowledge_base.jsonl")
    durable_learnings = [
        {"id": k.get("id"), "topic": k.get("topic"), "takeaway": k.get("takeaway"), "confidence": k.get("confidence")}
        for k in knowledge
    ]

    backlog = read_jsonl(state_dir, "hypothesis_backlog.jsonl")
    scored_candidates = sum(
        1 for c in backlog
        if c.get("status") == "candidate" and c.get("impact") is not None
        and c.get("confidence") is not None and c.get("ease") is not None
    )

    since = min((h.get("created_at") for h in hyps if h.get("created_at")), default=None)

    return json.dumps({
        "subsidiary_id": subsidiary_id,
        "since": since,
        "hypotheses_tried": len(hyps),
        "outcome_counts": outcome_counts,
        "resolved_count": resolved_count,
        "still_active_or_untested": still_open,
        "has_any_validated_build": outcome_counts["build"] > 0,
        "total_tokens_invested": total_tokens,
        "total_cost_usd": round(total_cost_usd, 4) if total_cost_usd else 0.0,
        "durable_learnings_count": len(durable_learnings),
        "durable_learnings": durable_learnings,
        "scored_backlog_candidates_count": scored_candidates,
    }, ensure_ascii=False)


def _persist_fix_streak(subsidiary_id: str, check_name: str, streak: int, extra: dict = None) -> None:
    subs, idx = _get_subsidiary(subsidiary_id)
    if idx is None:
        return
    streaks = subs[idx].get("fix_check_streaks") or {}
    streaks[check_name] = streak
    subs[idx]["fix_check_streaks"] = streaks
    if extra:
        subs[idx].update(extra)
    _write("subsidiaries.jsonl", subs)


def check_zero_state_streak(subsidiary_id: str, threshold: int) -> tuple:
    """Fires when a subsidiary has produced no new persisted state (no new
    hypothesis, knowledge_base entry, content draft, task-order progress, or
    backlog candidate/re-score) across `threshold` consecutive cycles -
    directly motivated by a real observed case, not a hypothetical.
    """
    subs, idx = _get_subsidiary(subsidiary_id)
    if idx is None:
        return False, {}
    sub = subs[idx]
    state_dir = _subsidiary_state_dir(sub, subsidiary_id)
    counts = {
        "hypotheses": len(read_jsonl(state_dir, "hypotheses.jsonl")),
        "knowledge_base": len(read_jsonl(state_dir, "knowledge_base.jsonl")),
        "content_drafts": len(read_jsonl(state_dir, "content_drafts.jsonl")),
        "task_orders": len(read_jsonl(state_dir, "task_orders.jsonl")),
        # Backlog addendum, Part 2: grooming the backlog (new candidates,
        # re-scored stale entries) is real, productive spare-capacity work
        # per section 2.3 - a cycle that only did that must not look
        # identical to one that did nothing at all.
        "hypothesis_backlog": len(read_jsonl(state_dir, "hypothesis_backlog.jsonl")),
    }
    last_counts = sub.get("_fix_last_state_counts")
    prior_streak = (sub.get("fix_check_streaks") or {}).get("zero_state_streak", 0)
    streak = prior_streak + 1 if last_counts == counts else 0
    _persist_fix_streak(subsidiary_id, "zero_state_streak", streak, {"_fix_last_state_counts": counts})
    if streak >= threshold:
        return True, {"streak_cycles": streak, "counts": counts}
    return False, {}


def check_recurring_malformed_tool_calls(subsidiary_id: str, cycle_signatures: list, threshold: int) -> tuple:
    """cycle_signatures: short strings (exception type + truncated message)
    for this cycle's malformed tool calls, passed in by crew.py since that
    list is cleared every cycle (crew.py's _malformed_tool_calls) - only a
    signature repeating cycle-over-cycle counts as "recurring", not two
    unrelated one-off errors.
    """
    subs, idx = _get_subsidiary(subsidiary_id)
    if idx is None:
        return False, {}
    sub = subs[idx]
    this_signature = cycle_signatures[0] if cycle_signatures else None
    last_signature = sub.get("_fix_last_malformed_signature")
    prior_streak = (sub.get("fix_check_streaks") or {}).get("malformed_tool_calls_streak", 0)
    streak = prior_streak + 1 if (this_signature and this_signature == last_signature) else (1 if this_signature else 0)
    _persist_fix_streak(
        subsidiary_id, "malformed_tool_calls_streak", streak, {"_fix_last_malformed_signature": this_signature}
    )
    if this_signature and streak >= threshold:
        return True, {"signature": this_signature, "streak_cycles": streak}
    return False, {}


def check_channel_bury_streak(subsidiary_id: str, threshold: int) -> tuple:
    subs, idx = _get_subsidiary(subsidiary_id)
    state_dir = _subsidiary_state_dir(subs[idx] if idx is not None else None, subsidiary_id)
    hyps = read_jsonl(state_dir, "hypotheses.jsonl")
    by_channel: dict = {}
    for h in hyps:
        channel = h.get("channel")
        if channel:
            by_channel.setdefault(channel, []).append(h)
    for channel, channel_hyps in by_channel.items():
        recent = channel_hyps[-threshold:]
        if len(recent) >= threshold and all(h.get("status") == "buried" for h in recent):
            return True, {
                "channel": channel, "bury_streak": threshold,
                "hypothesis_ids": [h.get("id") for h in recent],
            }
    return False, {}


def check_hypothesis_stuck_past_cap(subsidiary_id: str, subsidiary_policies: dict) -> tuple:
    """Only meaningful once max_duration_days_by_stage.status=='confirmed'
    (tools.py's own duration-cap enforcement follows the same rule) - while
    still 'proposed', there's no confirmed ceiling to be stuck past.
    """
    duration_policy = (subsidiary_policies or {}).get("max_duration_days_by_stage") or {}
    if duration_policy.get("status") != "confirmed":
        return False, {}
    caps = duration_policy.get("values") or {}
    subs, idx = _get_subsidiary(subsidiary_id)
    state_dir = _subsidiary_state_dir(subs[idx] if idx is not None else None, subsidiary_id)
    hyps = read_jsonl(state_dir, "hypotheses.jsonl")
    now = datetime.now(timezone.utc)
    for h in hyps:
        if h.get("status") != "active":
            continue
        cap = caps.get(h.get("evidence_stage"))
        created_at = h.get("created_at")
        if cap is None or not created_at:
            continue
        elapsed_days = (now - datetime.fromisoformat(created_at)).days
        if elapsed_days > cap and not h.get("duration_extension_approval_id"):
            return True, {
                "hypothesis_id": h.get("id"), "evidence_stage": h.get("evidence_stage"),
                "elapsed_days": elapsed_days, "cap_days": cap,
            }
    return False, {}


def check_repeated_pivot_streak(subsidiary_id: str, threshold: int) -> tuple:
    """The addendum calls this "repeated pivot-cap exhaustion" - this
    codebase has no literal pivot-cap counter, only the one-variable rule
    (tools.py write_hypothesis: every pivot names exactly one changed
    variable via pivot_variable_changed). The faithful real-data proxy: the
    last `threshold` resolved hypotheses (creation order) all resolving to
    outcome='pivot' - a subsidiary that keeps pivoting without ever reaching
    build/test_further/bury, the actual systemic non-convergence signal.
    """
    subs, idx = _get_subsidiary(subsidiary_id)
    state_dir = _subsidiary_state_dir(subs[idx] if idx is not None else None, subsidiary_id)
    hyps = read_jsonl(state_dir, "hypotheses.jsonl")
    resolved = [h for h in hyps if h.get("outcome")]
    recent = resolved[-threshold:]
    if len(recent) >= threshold and all(h.get("outcome") == "pivot" for h in recent):
        return True, {"hypothesis_ids": [h.get("id") for h in recent], "pivot_streak": threshold}
    return False, {}


def check_stale_approvals(threshold_hours: int) -> tuple:
    approvals = read_jsonl(SUBSIDIARY_STATE_DIR, "approval_queue.jsonl")
    now = datetime.now(timezone.utc)
    stale = []
    for a in approvals:
        if a.get("status") != "pending":
            continue
        created_at = a.get("created_at")
        if not created_at:
            continue
        age_hours = (now - datetime.fromisoformat(created_at)).total_seconds() / 3600
        if age_hours > threshold_hours:
            stale.append({"id": a.get("id"), "category": a.get("category"), "age_hours": round(age_hours, 1)})
    return (True, {"stale_approvals": stale}) if stale else (False, {})


def run_fix_checks(subsidiary_id: str, cycle_malformed_signatures: list = None) -> list:
    """Runs every Part-1.1 deterministic check for one subsidiary and
    returns the fired ones as [{"check_type": ..., "evidence": {...}}, ...].
    Pure/cheap, no LLM call - crew.py's cron loop calls this once per
    subsidiary per cycle, then escalates only the fired checks.
    """
    thresholds = read_fix_thresholds().get("values", {})
    subs, idx = _get_subsidiary(subsidiary_id)
    policies = (subs[idx].get("policies") if idx is not None else None) or {}

    checks = [
        ("zero_state_streak", check_zero_state_streak(subsidiary_id, thresholds.get("zero_state_streak_cycles", 3))),
        ("recurring_malformed_tool_calls", check_recurring_malformed_tool_calls(
            subsidiary_id, cycle_malformed_signatures or [], thresholds.get("malformed_tool_calls_cycles", 3)
        )),
        ("channel_bury_streak", check_channel_bury_streak(subsidiary_id, thresholds.get("channel_bury_streak", 3))),
        ("hypothesis_stuck_past_cap", check_hypothesis_stuck_past_cap(subsidiary_id, policies)),
        ("repeated_pivot_streak", check_repeated_pivot_streak(subsidiary_id, thresholds.get("repeated_pivot_streak", 2))),
        ("stale_approvals", check_stale_approvals(thresholds.get("stale_approval_hours", 48))),
    ]
    return [
        {"check_type": check_type, "evidence": evidence}
        for check_type, (did_fire, evidence) in checks
        if did_fire
    ]


def append_fix_md(entry_id: str, category: str, headline: str, body: str) -> None:
    """Append a new, dated section to the single, fixed STATE_DIR/_holding/
    FIX.md file - never a timestamped filename, never overwritten, never
    rewritten from scratch. This function's only side effects are this
    write and record_fix_entry below; there is no code path from here to
    any actual system/code change - a human must explicitly act on FIX.md
    in a separate Claude Code session (see this repo's CLAUDE.md for the
    retrieval instruction).
    """
    HOLDING_DIR.mkdir(parents=True, exist_ok=True)
    path = HOLDING_DIR / "FIX.md"
    dated = datetime.now(timezone.utc).isoformat()
    section = f"## [{entry_id}] {category}: {headline}\n{dated}\n\n{body}\n\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(section)


def record_fix_entry(entry_id: str, category: str, headline: str, subsidiary_id: str, check_type: str) -> None:
    _append("fix_entries.jsonl", {
        "id": entry_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "category": category, "headline": headline, "subsidiary_id": subsidiary_id,
        "check_type": check_type, "resolved": False, "resolved_at": None,
        "telegram_notified_at": None,
    })


def read_unnotified_fix_entries() -> list:
    return [e for e in _read("fix_entries.jsonl") if not e.get("resolved") and not e.get("telegram_notified_at")]


def mark_fix_entries_notified(entry_ids: list) -> None:
    if not entry_ids:
        return
    entries = _read("fix_entries.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    for e in entries:
        if e.get("id") in entry_ids:
            e["telegram_notified_at"] = now
    _write("fix_entries.jsonl", entries)


def resolve_fix_entry(entry_id: str) -> tuple:
    """Marks the sidecar record resolved and moves that entry's markdown
    section out of the live FIX.md into a dated archive file, so FIX.md
    stays lean and current instead of growing forever. Returns (ok, message)
    for the Telegram confirmation.
    """
    entries = _read("fix_entries.jsonl")
    idx = next((i for i, e in enumerate(entries) if e.get("id") == entry_id), None)
    if idx is None:
        return False, f"Kein FIX.md-Eintrag mit id '{entry_id}' gefunden."
    if entries[idx].get("resolved"):
        return False, f"{entry_id} ist bereits als geloest markiert."

    fix_path = HOLDING_DIR / "FIX.md"
    if fix_path.exists():
        text = fix_path.read_text(encoding="utf-8")
        header = f"## [{entry_id}]"
        start = text.find(header)
        if start != -1:
            next_header = text.find("\n## [", start + 1)
            section = text[start:] if next_header == -1 else text[start:next_header]
            remainder = text[:start] + (text[next_header:] if next_header != -1 else "")
            archive_path = HOLDING_DIR / f"FIX_resolved_{datetime.now(timezone.utc).date().isoformat()}.md"
            with archive_path.open("a", encoding="utf-8") as f:
                f.write(section)
            fix_path.write_text(remainder, encoding="utf-8")

    entries[idx]["resolved"] = True
    entries[idx]["resolved_at"] = datetime.now(timezone.utc).isoformat()
    _write("fix_entries.jsonl", entries)
    return True, f"{entry_id}: als geloest markiert und archiviert."


# --------------------------------------------------------------------------
# Kaizen (FIX.md/Kaizen/payment-propensity addendum, Part 2). One combined
# self-improvement report per cycle, assembled at the Main-CEO level from
# the Sub-CEO's own subsidiary-level points (already gathered in its status
# report, see file_status_report's kaizen_points field) merged with the
# Main-CEO's own holding-level observations - filed here as one call, not
# duplicated per agent. Two buckets: selbst_umsetzbar (already acted on or
# explicitly deferred, this cycle, strictly inside the existing Tier-0
# boundary - see the guardrail below) and fuer_aufsichtsrat (persisted,
# needs Jan). Every item in both buckets must cite a real id that actually
# exists in this subsidiary's current data - the same ground-truth
# discipline as the anti-copying tripwire (tools._instruction_echo_match),
# just enforced against real state instead of a fixed phrase list, since a
# citable id is a stronger and simpler proxy for "genuinely grounded" than
# matching known generic-advice phrasing.
# --------------------------------------------------------------------------

KAIZEN_ITEM_STATUSES = {"acted", "deferred"}
# Anything an action mentions from this list must go through
# fuer_aufsichtsrat + tools.request_approval, never selbst_umsetzbar -
# mirrors tools.APPROVAL_CATEGORIES exactly (spend/legal/publish/deploy/
# pricing) plus their common German phrasing, since Kaizen items are free
# text, not a structured category field.
_KAIZEN_TIER_KEYWORDS = [
    "spend", "ausgeben", "kaufen", "bezahlen", "payment", "zahlung", "budget",
    "publish", "veroeffentlichen", "veröffentlichen", "posten", "post ",
    "deploy", "deployen", "pricing", "preis setzen", "preisaenderung", "preisänderung",
    "legal", "vertrag", "contract",
]


def _kaizen_tier_violation(text: str) -> str:
    lowered = (text or "").lower()
    for kw in _KAIZEN_TIER_KEYWORDS:
        if kw in lowered:
            return kw.strip()
    return ""


def _kaizen_grounding_exists(subsidiary_id: str, grounding: str) -> bool:
    """A Kaizen item is grounded only if `grounding` is an id that actually
    exists in this subsidiary's real current data - not just a non-empty
    string.

    2026-08-14 calibration fix (Kaizen-must-produce-real-output addendum,
    Part E): originally only checked hypotheses.jsonl/channels.jsonl/the
    global approval_queue.jsonl. Confirmed real miscalibration - EVERY
    Kaizen attempt since it was built had been rejected, and the most
    common real, concrete, citable facts a cycle actually produces
    (a research finding, a knowledge-base takeaway, a backlog candidate)
    weren't in the checked id universe at all, even though they're exactly
    the kind of specific fact this check is meant to require. Broadened to
    the SAME grounding universe already established and validated
    elsewhere in this codebase (tools._backlog_grounding_exists) - not a
    new, separately-invented one - so a real research_finding/knowledge_
    base/backlog id is accepted, same discipline as ICE-score grounding.
    Still rejects ANY non-existent id - this widens what counts as a real
    fact, it does not relax the "must be real" requirement itself.
    """
    if not grounding:
        return False
    subs, idx = _get_subsidiary(subsidiary_id)
    state_dir = _subsidiary_state_dir(subs[idx] if idx is not None else None, subsidiary_id)
    ids = {h.get("id") for h in read_jsonl(state_dir, "hypotheses.jsonl")}
    ids |= {c.get("id") for c in read_jsonl(state_dir, "channels.jsonl")}
    ids |= {a.get("id") for a in read_jsonl(SUBSIDIARY_STATE_DIR, "approval_queue.jsonl")}
    ids |= {r.get("id") for r in read_jsonl(state_dir, "research_findings.jsonl")}
    ids |= {k.get("id") for k in read_jsonl(state_dir, "knowledge_base.jsonl")}
    ids |= {b.get("id") for b in read_jsonl(state_dir, "hypothesis_backlog.jsonl")}
    return grounding in ids


@tool("file_kaizen_report")
def file_kaizen_report(subsidiary_id: str, kaizen_report: str) -> str:
    """File THIS CYCLE's one consolidated Kaizen self-improvement report -
    called once per cycle by task_main_ceo_review, after merging the
    Sub-CEO's own subsidiary-level points (from its status report) with the
    Main-CEO's own holding-level observations. Never call this more than
    once per cycle, and never from the Sub-CEO's own task.

    kaizen_report is a JSON string: {"selbst_umsetzbar": [...],
    "fuer_aufsichtsrat": [...]} (either list may be empty, never omitted).

    selbst_umsetzbar items: {"action": str, "grounding": <a real
    hypothesis_id/channel_id/approval_id from THIS subsidiary's actual
    current data>, "status": "acted"|"deferred", "deferred_reason": str
    (required when status="deferred")}. HARD GUARDRAIL, enforced here, not
    just by instruction: action is rejected if it mentions spend/publish/
    deploy/pricing/legal in any form - anything touching those belongs in
    fuer_aufsichtsrat + tools.request_approval only. Kaizen must never
    become a backdoor around the existing Tier boundary
    (tools.APPROVAL_CATEGORIES).

    fuer_aufsichtsrat items: {"suggestion": str, "grounding": <same
    real-id requirement>} - persisted to kaizen_suggestions.jsonl every
    cycle they occur, surfaced via a short Telegram pointer only (see
    crew.py's _aufsichtsrat_lines) - never the full suggestion text
    repeated in chat every cycle.

    Every item in both buckets is rejected unless `grounding` names an id
    that genuinely exists right now in this subsidiary's hypotheses.jsonl/
    channels.jsonl or the global approval_queue.jsonl - generic startup
    advice with no cited fact behind it doesn't get filed.
    """
    try:
        report = json.loads(kaizen_report)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"kaizen_report must be a JSON string: {exc}"})
    if not isinstance(report, dict):
        return json.dumps({"error": "kaizen_report must be a JSON object"})

    selbst_umsetzbar = report.get("selbst_umsetzbar")
    fuer_aufsichtsrat = report.get("fuer_aufsichtsrat")
    if not isinstance(selbst_umsetzbar, list) or not isinstance(fuer_aufsichtsrat, list):
        return json.dumps({
            "error": "kaizen_report needs 'selbst_umsetzbar' and 'fuer_aufsichtsrat' as lists (may be empty)"
        })

    for item in selbst_umsetzbar:
        if not isinstance(item, dict) or not (item.get("action") or "").strip():
            return json.dumps({"error": "every selbst_umsetzbar item needs a non-empty 'action'"})
        if not _kaizen_grounding_exists(subsidiary_id, item.get("grounding", "")):
            return json.dumps({
                "error": f"selbst_umsetzbar item's grounding '{item.get('grounding')}' is not a real "
                         "hypothesis/channel/approval id from this subsidiary's current data - generic "
                         "advice without a cited fact isn't accepted"
            })
        violation = _kaizen_tier_violation(item["action"])
        if violation:
            return json.dumps({
                "error": f"selbst_umsetzbar item's action mentions '{violation}' - anything touching spend/"
                         "publish/deploy/pricing/legal belongs in fuer_aufsichtsrat only, never "
                         "selbst_umsetzbar (Kaizen never expands what's autonomous)"
            })
        if item.get("status") not in KAIZEN_ITEM_STATUSES:
            return json.dumps({"error": "selbst_umsetzbar item's status must be 'acted' or 'deferred'"})
        if item["status"] == "deferred" and not (item.get("deferred_reason") or "").strip():
            return json.dumps({"error": "a 'deferred' selbst_umsetzbar item needs a non-empty deferred_reason"})

    for item in fuer_aufsichtsrat:
        if not isinstance(item, dict) or not (item.get("suggestion") or "").strip():
            return json.dumps({"error": "every fuer_aufsichtsrat item needs a non-empty 'suggestion'"})
        if not _kaizen_grounding_exists(subsidiary_id, item.get("grounding", "")):
            return json.dumps({
                "error": f"fuer_aufsichtsrat item's grounding '{item.get('grounding')}' is not a real "
                         "hypothesis/channel/approval id from this subsidiary's current data - generic "
                         "advice without a cited fact isn't accepted"
            })

    now = datetime.now(timezone.utc).isoformat()
    for item in fuer_aufsichtsrat:
        _append("kaizen_suggestions.jsonl", {
            "id": f"kaizen_{uuid.uuid4().hex[:8]}", "created_at": now, "subsidiary_id": subsidiary_id,
            "suggestion": item["suggestion"], "grounding": item["grounding"], "telegram_notified_at": None,
        })

    # cycle-reporting/backlog addendum, Part 1.1: selbst_umsetzbar items used
    # to only exist in this call's own return value - never persisted, so
    # nothing later could ever ask "what did Kaizen actually act on this
    # cycle" except by re-reading the agent's own free-text task output.
    # kaizen_actions.jsonl closes that gap without touching kaizen_
    # suggestions.jsonl's existing shape/consumers (still exclusively the
    # fuer_aufsichtsrat/board-facing bucket).
    for item in selbst_umsetzbar:
        _append("kaizen_actions.jsonl", {
            "id": f"kaizenact_{uuid.uuid4().hex[:8]}", "created_at": now, "subsidiary_id": subsidiary_id,
            "action": item["action"], "grounding": item["grounding"], "status": item["status"],
            "deferred_reason": item.get("deferred_reason"),
        })

    return json.dumps({
        "logged_selbst_umsetzbar": len(selbst_umsetzbar), "logged_fuer_aufsichtsrat": len(fuer_aufsichtsrat),
    })


def read_unnotified_kaizen_suggestions() -> list:
    return [e for e in _read("kaizen_suggestions.jsonl") if not e.get("telegram_notified_at")]


def read_kaizen_suggestions(subsidiary_id: str = "", since_iso: str = "") -> list:
    """Read the fuer_aufsichtsrat/board-facing bucket, optionally scoped to
    one subsidiary and/or a since_iso cutoff - unlike read_unnotified_
    kaizen_suggestions (Telegram-notification-state scoped), this is for
    "what did this cycle actually propose to the board", regardless of
    whether the Telegram pointer already fired.
    """
    suggestions = _read("kaizen_suggestions.jsonl")
    if subsidiary_id:
        suggestions = [s for s in suggestions if s.get("subsidiary_id") == subsidiary_id]
    if since_iso:
        suggestions = [s for s in suggestions if (s.get("created_at") or "") > since_iso]
    return suggestions


def read_kaizen_actions(subsidiary_id: str = "", since_iso: str = "") -> list:
    """Read the selbst_umsetzbar (acted/deferred) bucket persisted by
    file_kaizen_report - used by crew.py's business-report assembly (Part
    1.1) to show what Kaizen actually acted on this cycle vs what it
    deferred, mechanically split from the fuer_aufsichtsrat/board bucket
    (kaizen_suggestions.jsonl) rather than trusted to the agent's own prose.
    """
    actions = _read("kaizen_actions.jsonl")
    if subsidiary_id:
        actions = [a for a in actions if a.get("subsidiary_id") == subsidiary_id]
    if since_iso:
        actions = [a for a in actions if (a.get("created_at") or "") > since_iso]
    return actions


def mark_kaizen_suggestions_notified(entry_ids: list) -> None:
    if not entry_ids:
        return
    entries = _read("kaizen_suggestions.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    for e in entries:
        if e.get("id") in entry_ids:
            e["telegram_notified_at"] = now
    _write("kaizen_suggestions.jsonl", entries)


# --------------------------------------------------------------------------
# Structured handoff: Sub-CEO -> Main-CEO (status report). Generalizes the
# pivot-proposal pattern to every cycle's report, not just fundamental-
# strategy escalations - a fixed record instead of the Main-CEO having to
# re-derive what happened from the Sub-CEO's free-text task output.
# --------------------------------------------------------------------------

@tool("file_status_report")
def file_status_report(
    subsidiary_id: str,
    what_was_asked: str,
    what_was_found: str,
    hypothesis_id: str = "",
    outcome: str = "",
    needs_decision_from_above: bool = False,
    decision_context: str = "",
    kaizen_points: str = "",
) -> str:
    """Sub-CEO reports this cycle's work to the Main-CEO as a fixed record -
    what was being worked on, what was found, and whether anything needs a
    decision from above. Pass hypothesis_id and outcome (one of build/
    test_further/pivot/bury) whenever this report is about a specific
    hypothesis's result. If needs_decision_from_above is true,
    decision_context is required - say plainly what the Main-CEO actually
    needs to decide, not just that something happened.

    kaizen_points (Kaizen addendum, Part 2): optional JSON string, a list of
    this cycle's subsidiary-level self-improvement observations, e.g.
    [{"observation": str, "grounding": <a real hypothesis/channel/approval
    id from this cycle's own data>}]. The Main-CEO reads this every cycle
    (via read_status_reports) and merges it with its own holding-level
    observations into the ONE consolidated report filed via
    file_kaizen_report - never file a separate Kaizen report from here.
    """
    if needs_decision_from_above and not decision_context.strip():
        return json.dumps({"error": "needs_decision_from_above=true requires a non-empty decision_context"})
    if outcome and outcome not in HYPOTHESIS_OUTCOMES:
        return json.dumps({"error": f"invalid outcome '{outcome}', must be one of {sorted(HYPOTHESIS_OUTCOMES)} or empty"})
    parsed_kaizen_points = []
    if kaizen_points:
        try:
            parsed_kaizen_points = json.loads(kaizen_points)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"kaizen_points must be a JSON string (list): {exc}"})
        if not isinstance(parsed_kaizen_points, list):
            return json.dumps({"error": "kaizen_points must be a JSON list"})

    record = {
        "id": f"report_{uuid.uuid4().hex[:8]}",
        "subsidiary_id": subsidiary_id,
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "hypothesis_id": hypothesis_id or None,
        "what_was_asked": what_was_asked,
        "what_was_found": what_was_found,
        "outcome": outcome or None,
        "needs_decision_from_above": needs_decision_from_above,
        "decision_context": decision_context or None,
        "acknowledged": False,
        "kaizen_points": parsed_kaizen_points,
    }
    _append("status_reports.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_status_reports")
def read_status_reports(subsidiary_id: str = "", needs_decision_only: bool = False) -> str:
    """Read Sub-CEO status reports. Pass subsidiary_id to scope to one
    subsidiary, or "" for all. Pass needs_decision_only=true to see just the
    reports actually waiting on a Main-CEO decision, instead of every
    routine "nothing to report" cycle.
    """
    reports = _read("status_reports.jsonl")
    if subsidiary_id:
        reports = [r for r in reports if r.get("subsidiary_id") == subsidiary_id]
    if needs_decision_only:
        reports = [r for r in reports if r.get("needs_decision_from_above")]
    return json.dumps(reports, ensure_ascii=False)


@tool("acknowledge_status_report")
def acknowledge_status_report(report_id: str) -> str:
    """Mark a status report as reviewed, so the same report doesn't keep
    showing up as needing attention cycle after cycle.
    """
    # See route_idea's comment: acknowledged in a batch ("for each you've
    # actually considered"), needs the lock held across the whole
    # read->mutate->write span.
    with _jsonl_lock("status_reports.jsonl"):
        reports = _read("status_reports.jsonl")
        idx = next((i for i, r in enumerate(reports) if r.get("id") == report_id), None)
        if idx is None:
            return json.dumps({"error": f"no status report with id '{report_id}'"})
        reports[idx]["acknowledged"] = True
        _write("status_reports.jsonl", reports)
    return json.dumps({"ok": True, "id": report_id})


# --------------------------------------------------------------------------
# Board-question channel (self-development addendum, Part F item 2). Real
# gap this closes: the system previously had no way to raise genuine open
# strategic uncertainty back to Jan that doesn't fit request_approval
# (needs sign-off on a specific action), file_pivot_proposal (a specific
# decision type: approve_in_place/move_to_subsidiary/spinoff_required/
# rejected), or file_stage_skip_request (a specific evidence-stage
# exception) - e.g. "the backlog surfaced two genuinely different,
# well-scored problem directions; do you have a preference before we
# commit testing capacity to one" has no honest home in any of those
# three. Both ceo_agent and main_ceo_agent get this tool (crew.py) -
# genuine open uncertainty can come from either role, not just the
# Main-CEO. Deliberately NOT a way to avoid an ordinary Tier-0 judgment
# call either agent is already equipped to make on its own - instructed
# accordingly in crew.py's task descriptions.
# --------------------------------------------------------------------------

BOARD_QUESTION_STATUSES = {"open", "answered"}


@tool("file_board_question")
def file_board_question(subsidiary_id: str, question: str, context: str, filed_by: str) -> str:
    """File a genuine open strategic question for Jan/the board - distinct
    from request_approval (needs sign-off on a specific action already
    decided), file_pivot_proposal (a specific pivot decision type), and
    file_stage_skip_request (a specific evidence-stage exception). Use this
    only for genuine open uncertainty that doesn't fit those boxes - never
    as a way to avoid an ordinary judgment call you're already equipped to
    make on your own.

    question: the actual question, one clear sentence.
    context: why this matters right now and what's genuinely at stake -
    say plainly what's uncertain and why it's not a call you can make
    alone, not just background narration.
    filed_by: one of 'sub_ceo'/'main_ceo' - which role is asking.

    Surfaced as a short pointer in the Telegram "Fuer den Aufsichtsrat"
    section (never the full question/context repeated in chat every
    cycle) until answered via the 'board_answer: <id> <answer text>'
    Telegram command.
    """
    if not question.strip():
        return json.dumps({"error": "question must not be empty"})
    if not context.strip():
        return json.dumps({"error": "context must not be empty - say plainly why this is genuinely uncertain"})
    if filed_by not in ("sub_ceo", "main_ceo"):
        return json.dumps({"error": "filed_by must be one of 'sub_ceo'/'main_ceo'"})
    record = {
        "id": f"boardq_{uuid.uuid4().hex[:8]}",
        "subsidiary_id": subsidiary_id,
        "question": question,
        "context": context,
        "filed_by": filed_by,
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "answer": None,
        "answered_at": None,
        "telegram_notified_at": None,
    }
    _append("board_questions.jsonl", record)
    return json.dumps({"ok": True, "id": record["id"]})


@tool("read_board_questions")
def read_board_questions(subsidiary_id: str = "", status: str = "") -> str:
    """Read filed board questions, optionally scoped to one subsidiary
    and/or status ('open'/'answered'). Check this before filing a new
    question on the same genuine uncertainty - don't refile one that's
    already open and unanswered.
    """
    questions = _read("board_questions.jsonl")
    if subsidiary_id:
        questions = [q for q in questions if q.get("subsidiary_id") == subsidiary_id]
    if status:
        questions = [q for q in questions if q.get("status") == status]
    return json.dumps(questions, ensure_ascii=False)


def read_unnotified_board_questions() -> list:
    """Orchestration-level (crew.py's Telegram surfacing), same pattern as
    read_unnotified_kaizen_suggestions: open questions never yet pointed to
    in a Telegram report.
    """
    return [q for q in _read("board_questions.jsonl") if q.get("status") == "open" and not q.get("telegram_notified_at")]


def mark_board_questions_notified(question_ids: list) -> None:
    if not question_ids:
        return
    with _jsonl_lock("board_questions.jsonl"):
        questions = _read("board_questions.jsonl")
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for q in questions:
            if q.get("id") in question_ids and not q.get("telegram_notified_at"):
                q["telegram_notified_at"] = now
                changed = True
        if changed:
            _write("board_questions.jsonl", questions)


def answer_board_question(question_id: str, answer: str) -> dict:
    """Telegram command 'board_answer: <id> <answer text>' - only reachable
    via _apply_telegram_commands (crew.py), same tier as fix_resolved:/
    stagnation_ack:, never an agent tool: answering is Jan's call, not
    something either agent can do to itself.
    """
    if not answer.strip():
        return {"error": "answer must not be empty"}
    with _jsonl_lock("board_questions.jsonl"):
        questions = _read("board_questions.jsonl")
        idx = next((i for i, q in enumerate(questions) if q.get("id") == question_id), None)
        if idx is None:
            return {"error": f"no board question with id '{question_id}'"}
        if questions[idx].get("status") != "open":
            return {"error": f"'{question_id}' is already '{questions[idx].get('status')}', not re-answering"}
        questions[idx]["status"] = "answered"
        questions[idx]["answer"] = answer
        questions[idx]["answered_at"] = datetime.now(timezone.utc).isoformat()
        _write("board_questions.jsonl", questions)
        return {"ok": True, "id": question_id}


# --------------------------------------------------------------------------
# Structured handoff: Main-CEO -> Sub-CEO (strategic direction). The
# reverse of the above - previously there was no channel at all for the
# Main-CEO to proactively steer a Sub-CEO; it could only ever react to what
# came up to it. A genuinely new capability for main_ceo_agent, not an
# extension of an existing one.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Mechanical stale-direction detection (stale-strategic-direction addendum,
# Part A). Confirmed twice now, in two separate real cycles: an instruction
# telling the Main-CEO to recognize and replace a stale direction (one
# still carrying hardcoded economics figures from the buried
# hyp_bootstrap_001 era) was implemented in prompt text, and twice the
# Main-CEO's own live judgment decided the existing direction was fine as
# it stood. Stop trying to fix this through better instructions - this is
# a deterministic, regex-only check with no LLM judgment involved, run in
# crew.py's plain Python __main__ loop BEFORE task_main_ceo_review's own
# LLM call ever runs, so the agent never even sees the stale version.
# --------------------------------------------------------------------------

_STALE_STRATEGIC_DIRECTION_PATTERNS = (
    re.compile(r"\$\d+(?:\.\d+)?(?:\s*/\s*mo(?:nth)?)?"),  # hardcoded dollar figures, e.g. $49, $49/mo
    re.compile(r"\b\d+\+?\s*conversions?\b", re.IGNORECASE),  # "51+ conversions"/"51 conversions"
)


def is_stale_strategic_direction(focus_area: str) -> bool:
    """True if focus_area contains one of the known-stale content markers
    (a hardcoded dollar figure or conversion count) left over from the
    buried hyp_bootstrap_001 era - a specific, known string pattern, never
    a model judging whether a direction "seems" stale.
    """
    text = focus_area or ""
    return any(pattern.search(text) for pattern in _STALE_STRATEGIC_DIRECTION_PATTERNS)


def corrected_strategic_direction_focus_area(subsidiary_id: str) -> str:
    """The replacement content forced in when is_stale_strategic_direction
    fires: problem-first, no specific hardcoded numbers, monetization
    stated as the eventual required filter every hypothesis must clear -
    never a stated precondition or a specific figure baked into the
    direction itself. Parametrized by subsidiary_id rather than hardcoding
    any one subsidiary's business specifics (competitor-research addendum,
    item 3 - the same discipline applied here for a new piece of text).
    """
    return (
        f"Validate a real problem worth solving for {subsidiary_id}'s actual "
        "target users - problem-first, not economics-first. Monetization "
        "(break-even economics, defensibility, pricing) stays the required "
        "filter every hypothesis must clear before it can reach build, but "
        "it is evaluated per-hypothesis once real evidence exists, never "
        "assumed or fixed in advance as a specific number in this "
        "direction itself. Chasing revenue directly over solving the "
        "actual problem is not the goal."
    )


@tool("set_strategic_direction")
def set_strategic_direction(subsidiary_id: str, focus_area: str, reasoning: str) -> str:
    """Main-CEO sets the current strategic direction/focus for a
    subsidiary's Sub-CEO - e.g. "prioritize value-hypotheses over growth
    experiments this quarter" or "hold off on paid channels until the
    pivot proposal is decided". This does not override the Sub-CEO's own
    tactical judgment (channel picks, hypothesis sizing) - it's the
    higher-level frame the Sub-CEO should read and factor in, not a command
    the Sub-CEO tools enforce mechanically.
    """
    record = {
        "id": f"dir_{uuid.uuid4().hex[:8]}",
        "subsidiary_id": subsidiary_id,
        "set_at": datetime.now(timezone.utc).isoformat(),
        "focus_area": focus_area,
        "reasoning": reasoning,
    }
    _append("strategic_directions.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_strategic_direction")
def read_strategic_direction(subsidiary_id: str) -> str:
    """Read the current (most recently set) strategic direction for this
    subsidiary from the Main-CEO, or null if none has ever been set - no
    direction set is a normal, valid state, not an error.
    """
    directions = [d for d in _read("strategic_directions.jsonl") if d.get("subsidiary_id") == subsidiary_id]
    if not directions:
        return json.dumps({"direction": None})
    # Last in append order is the most recently written one - more reliable
    # than sorting on set_at, which two calls in the same tick can tie on.
    return json.dumps({"direction": directions[-1]}, ensure_ascii=False)
