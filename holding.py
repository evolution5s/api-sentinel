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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from crewai.tools import tool

from jsonl_store import append_jsonl, read_jsonl, write_jsonl
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
# plainly on every subsidiary record rather than left implicit (audit
# addendum, section 4): register_subsidiary only ever writes a registry row.
# There is currently no per-subsidiary state partition anywhere in tools.py
# (STATE_DIR is one single module-level path, hypotheses.jsonl/channels.jsonl
# carry no subsidiary_id field) and no code path that would instantiate a
# second Sub-CEO/Growth/Dev crew - crew.py wires up exactly one Crew, hardcoded
# to api-sentinel throughout its task text. A registered subsidiary beyond the
# first is bookkeeping only until a human does the separate engineering work
# (its own STATE_DIR/service, its own crew wiring) - never assume or imply
# otherwise when reporting on the registry.
NEW_SUBSIDIARY_CAPABILITY_NOTE = (
    "registry_only - no isolated state directory or operative Sub-CEO/Growth/"
    "Dev crew exists for this subsidiary yet; register_subsidiary only creates "
    "this metadata row. Running real hypotheses under this id requires "
    "separate human engineering work first (its own STATE_DIR, its own crew "
    "wiring in crew.py) - until then, do not route operative work here."
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
        "state_dir": str(SUBSIDIARY_STATE_DIR),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status_history": [],
        "policies": dict(SUBSIDIARY_POLICY_DEFAULTS),
        "notes": (
            "Pre-existing subsidiary, auto-registered when the Main-CEO/"
            "Sub-CEO holding layer was added."
        ),
    }


def _all_subsidiaries() -> list:
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
    only creates the metadata entry - it does NOT provision any actual
    infrastructure (a new Railway service, its own crew/agents); that stays
    separate, human-directed engineering work once approved. Requires
    approved_request_id pointing at an approved entry in the existing human
    approval queue - reuses request_approval, no separate gate. Never
    fabricate an approval id; if none exists yet, file request_approval
    first and wait.
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
        "state_dir": None,
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
    Not itself an escalation trigger and files nothing - weigh it in your
    own judgment alongside everything else this cycle, same as any other
    read-only tool's output, and never treat a 'build' outcome here as
    proof of real progress on its own if the underlying hypothesis never
    actually validated a genuine user problem.
    """
    subs = _all_subsidiaries()
    sub = next((s for s in subs if s.get("id") == subsidiary_id), None)
    if sub is None:
        return json.dumps({"error": f"no subsidiary with id '{subsidiary_id}'"})

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

    return json.dumps({
        "subsidiary_id": subsidiary_id,
        "outcome_counts": outcome_counts,
        "resolved_count": resolved_count,
        "possible_stall": possible_stall,
        "reason": (
            f"{resolved_count} hypotheses resolved (build/pivot/bury), 0 reached 'build' yet"
            if possible_stall else
            f"{resolved_count} resolved so far, {build_count} reached 'build'"
        ),
    })


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
) -> str:
    """Sub-CEO reports this cycle's work to the Main-CEO as a fixed record -
    what was being worked on, what was found, and whether anything needs a
    decision from above. Pass hypothesis_id and outcome (one of build/
    test_further/pivot/bury) whenever this report is about a specific
    hypothesis's result. If needs_decision_from_above is true,
    decision_context is required - say plainly what the Main-CEO actually
    needs to decide, not just that something happened.
    """
    if needs_decision_from_above and not decision_context.strip():
        return json.dumps({"error": "needs_decision_from_above=true requires a non-empty decision_context"})
    if outcome and outcome not in HYPOTHESIS_OUTCOMES:
        return json.dumps({"error": f"invalid outcome '{outcome}', must be one of {sorted(HYPOTHESIS_OUTCOMES)} or empty"})

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
    reports = _read("status_reports.jsonl")
    idx = next((i for i, r in enumerate(reports) if r.get("id") == report_id), None)
    if idx is None:
        return json.dumps({"error": f"no status report with id '{report_id}'"})
    reports[idx]["acknowledged"] = True
    _write("status_reports.jsonl", reports)
    return json.dumps({"ok": True, "id": report_id})


# --------------------------------------------------------------------------
# Structured handoff: Main-CEO -> Sub-CEO (strategic direction). The
# reverse of the above - previously there was no channel at all for the
# Main-CEO to proactively steer a Sub-CEO; it could only ever react to what
# came up to it. A genuinely new capability for main_ceo_agent, not an
# extension of an existing one.
# --------------------------------------------------------------------------

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
