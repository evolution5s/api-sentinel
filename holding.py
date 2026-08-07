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
from tools import STATE_DIR as SUBSIDIARY_STATE_DIR

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

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "state_dir": None,
        "status_history": [{
            "at": datetime.now(timezone.utc).isoformat(), "to": "active",
            "reason": f"spin-off approved via {approved_request_id}",
        }],
        **patch,
    }
    subs.append(record)
    _write("subsidiaries.jsonl", subs)
    return json.dumps({"ok": True, "id": patch["id"]})


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
