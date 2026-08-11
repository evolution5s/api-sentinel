"""State persistence and CrewAI tools shared by all api-sentinel agents.

This is the Sub-CEO/operative layer for the api-sentinel subsidiary
specifically - see holding.py for the Main-CEO/holding-level layer above it
(subsidiary registry, pivot review, cross-subsidiary requests, the shared
research archive), which reuses STATE_DIR's approval_queue.jsonl as the one
human approval queue rather than adding a second one.

Everything under STATE_DIR is plain JSONL so it stays human-readable and
diffable. STATE_DIR defaults to /data - on Railway this is only durable
across cron ticks if an actual Railway Volume is attached and mounted
there (confirmed live via `railway volume list`: a "data" volume, status
"Ready", mounted at /data - not something to assume from this default
alone, since a plain, volume-less path at the same location would look
identical from inside the container until the next redeploy wipes it).
Locally/in tests, STATE_DIR is just an ordinary directory, no volume
involved - override via the STATE_DIR env var.
"""
import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from crewai.tools import tool

import scoring
from jsonl_store import append_jsonl, read_jsonl, write_jsonl

STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))

GITHUB_REPO = "evolution5s/api-sentinel"
GITHUB_API = "https://api.github.com"
SIGNUP_TITLE_PREFIX = "[Signup]"

APPROVAL_CATEGORIES = {"spend", "legal", "publish", "deploy", "pricing"}
# Rigid structured template for category='publish' requests (structural-
# rebuild addendum, section 7) - no narrative prose in the fields that
# matter, rendered verbatim in Telegram, never reflowed.
PUBLISH_TEMPLATE_FIELDS = {
    "platform", "target_url", "title", "text", "footer",
    "hypothesis_id", "evidence_stage", "is_experiment", "success_criterion",
}
#
# Structural-rebuild addendum (section 2): fields are now required based on
# reversibility, not a single flat checklist. research/community_engagement
# are two-way doors (Bezos 1997) - cheap, fast, reversible, decide and move,
# no load-bearing economics needed yet. landing_page/build cross toward a
# one-way door - real cost, real commitment - so the numbers backing them
# must be precise and evidence-grounded by then.
BASE_REQUIRED_HYPOTHESIS_FIELDS = {
    "id", "statement", "category", "landing_page_variant_id",
    "failure_rate", "success_rate", "duration_days", "channel",
    "hypothesis_type",
    # Bullseye-style ranking signal (four-fixes addendum, point 4) - same
    # shape as channels.jsonl's impact_score/confidence_score, so competing
    # hypothesis ideas can be ranked against each other the same way
    # competing channels already are, instead of as a disconnected pass.
    "impact_score", "confidence_score",
    # No longer optional (was under the audit addendum) - every hypothesis
    # now declares its own evidence stage from the start, since required-ness
    # of everything else below hinges on it.
    "evidence_stage",
}
# Only required once evidence_stage crosses into landing_page/build - see
# the module-level comment above. AI-native economics addendum: the
# estimate itself must be justified, not just present - see the ceiling
# check below.
STAGE_GATED_ECONOMICS_FIELDS = {
    "estimated_build_cost", "price_point_monthly",
    "break_even_horizon_months", "break_even_users", "build_cost_reasoning",
}
# Union, for anything that wants "everything write_hypothesis could ever
# require" rather than the stage-conditional split (e.g. a full-coverage
# test fixture at build stage).
REQUIRED_HYPOTHESIS_FIELDS = BASE_REQUIRED_HYPOTHESIS_FIELDS | STAGE_GATED_ECONOMICS_FIELDS
# Required (section 3) whenever evidence_stage is being set to "research":
# the research plan - what question this is meant to answer, and what
# would count as confirming vs. disconfirming - logged before research
# starts, not reconstructed afterward to fit whatever was found.
RESEARCH_PLAN_FIELDS = {
    "research_objective", "research_confirming_criteria", "research_disconfirming_criteria",
}
HYPOTHESIS_TYPES = {"value", "growth"}
PIVOT_VARIABLES = {"audience", "price", "copy", "channel", "timing"}
HYPOTHESIS_STATUSES = {"active", "evaluated", "buried"}
# Same capping logic as MAX_CHANNELS_TESTING below - spreading thin across
# too many hypotheses at once is the more common failure than picking the
# wrong one first (four-fixes addendum, point 4).
MAX_ACTIVE_HYPOTHESES = 3
# Evidence-stage ladder: ordered from cheapest/weakest to most expensive/
# strongest signal. Every hypothesis now declares one (BASE_REQUIRED_
# HYPOTHESIS_FIELDS) from the start. research/community_engagement are
# two-way doors - real, substantive artifacts are still required to progress
# past them (see the artifact-gate helpers below), but the bar is "did you
# actually do the cheap step", not committee review. landing_page/build are
# where real cost gets committed - crossing into either without artifact-
# backed history through the earlier stages requires a Main-CEO-reviewed
# stage-skip request (holding.py's stage_skip_requests.jsonl), not a
# self-written excuse - the prior self-signed stage_justification string was
# exactly how the bootstrap hypothesis skipped straight to a landing page.
EVIDENCE_STAGES = ["research", "community_engagement", "landing_page", "build"]
EVIDENCE_EARLY_STAGES = {"research", "community_engagement"}
EVIDENCE_LATER_STAGES = {"landing_page", "build"}
# Substance-length gate for a research_findings.jsonl entry to count as a
# real artifact (section 3) - same pattern/rationale as
# BUILD_COST_JUSTIFICATION_MIN_LENGTH below: a one-liner doesn't qualify as
# "here's what was actually found."
RESEARCH_FINDING_MIN_LENGTH = 80
# Payment-propensity-and-size addendum (Part 3): how long a channel's
# knowledge_base payment-propensity verdict (topic="payment propensity
# scan") stays reusable before a fresh scan is worth the tokens. A cache-
# freshness knob, not a decision with real consequences the way duration
# caps/FIX thresholds are - deliberately a plain constant, not a third
# parallel Telegram-confirmable governance parameter.
PAYMENT_PROPENSITY_STALENESS_DAYS = 90
# A community_engagement-stage artifact must be one of these post_types
# (section 4) - an actual thread reply or a genuine question post, not
# passive lurking.
_COMMUNITY_ENGAGEMENT_POST_TYPES = {"thread_reply", "own_question_post"}
# AI-native economics addendum: this system's builds happen via Dev-agent
# LLM calls, not a human dev team or agency - a landing page/signup form/
# small webhook should cost token spend (very low single digits), not
# market-rate thousands. Anything above this ceiling for a Dev-buildable
# artifact of that scope needs a real, substantive justification (more
# files/integration points/iteration passes - genuine added token/
# iteration volume, not a return to agency-rate thinking), enforced as a
# minimum reasoning length rather than a hard block, since a genuinely
# more involved build is a real, if rarer, case.
SIMPLE_BUILD_COST_CEILING = 10.0
BUILD_COST_JUSTIFICATION_MIN_LENGTH = 80
CHANNEL_STATUSES = {"not_tested", "bench", "testing", "retired"}
MAX_CHANNELS_TESTING = 3
MAX_TOTAL_CHANNELS = 20
REQUIRED_CHANNEL_FIELDS = {"id", "name", "category", "is_paid", "impact_score", "confidence_score"}

# Structural-rebuild addendum, section 2: no longer a hardcoded assumption
# that this module only ever serves one subsidiary. crew.py calls
# set_active_subsidiary(subsidiary_id) once per subsidiary before each
# crew.kickoff() (today: once, "api-sentinel" - a second subsidiary doesn't
# need a second Railway service, just a second pass through this same
# process with the context switched). Every subsidiary-scoped read/write
# below resolves against whichever subsidiary is currently active; tests
# and any other direct caller can read/pass this explicitly instead.
DEFAULT_SUBSIDIARY_ID = "api-sentinel"
_active_subsidiary_id = DEFAULT_SUBSIDIARY_ID


def set_active_subsidiary(subsidiary_id: str) -> None:
    """Switch which subsidiary's data STATE_DIR reads/writes resolve
    against for the remainder of this process. Orchestration-level, not an
    agent tool - called once per subsidiary from crew.py's __main__, same
    category as check_state_persistence.
    """
    global _active_subsidiary_id
    _active_subsidiary_id = subsidiary_id


def get_active_subsidiary() -> str:
    return _active_subsidiary_id
# Section 6 (structural-rebuild addendum): duration caps are a board/
# Aufsichtsrat policy call, not a number this addendum dictates - that
# would repeat the same mistake as the old hardcoded economics. This is
# only ever a *proposed* starting point (status='proposed') until a human
# confirms or adjusts it via Telegram ('duration_policy: confirm' or
# 'duration_policy: <research> <community_engagement> <landing_page> <build>'
# in days, 'none' for build) - see _apply_telegram_commands. Never activated
# silently: write_hypothesis's duration_days ceiling check only fires once
# status=='confirmed'.
DEFAULT_PROPOSED_DURATION_CAPS = {
    "status": "proposed",
    "values": {"research": 3, "community_engagement": 5, "landing_page": 14, "build": None},
    "note": (
        "Proposed starting point, not yet enforced - confirm or adjust via Telegram "
        "('duration_policy: confirm', or 'duration_policy: <research> <community_engagement> "
        "<landing_page> <build>' in days, 'none' for build)."
    ),
}
_SUBSIDIARY_POLICY_DEFAULTS = {
    "paid_channels_allowed": False,
    "cold_email_allowed": False,
    "data_collection_allowed": False,
    "risk_tolerance": "low",
    "max_duration_days_by_stage": DEFAULT_PROPOSED_DURATION_CAPS,
}


def _read_own_policies() -> dict:
    """Conservative-by-default read of the *active* subsidiary's (see
    set_active_subsidiary) holding-level policies (paid_channels_allowed
    etc., set via Main-CEO's update_subsidiary_policies). Falls back to the
    same conservative defaults holding.py bootstraps with if the holding
    registry doesn't exist yet, so this never blocks on init order between
    the two layers.
    """
    subs = read_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl")
    sub = next((s for s in subs if s.get("id") == _active_subsidiary_id), None)
    if sub is None:
        return dict(_SUBSIDIARY_POLICY_DEFAULTS)
    return {**_SUBSIDIARY_POLICY_DEFAULTS, **(sub.get("policies") or {})}


# --------------------------------------------------------------------------
# STATE_DIR / JSONL primitives (structural-rebuild addendum, section 2).
# Subsidiary-scoped files (hypotheses/channels/task_orders/content_drafts/
# signups/research_findings/knowledge_base/usage_history) now live under
# STATE_DIR/<subsidiary_id>/ - the same pattern already used for holding-
# level state under STATE_DIR/_holding/, just one level per subsidiary
# instead of one level for the holding. approval_queue.jsonl and the
# system-level files (system_paused.json, telegram_update_offset.txt,
# last_cycle_note.txt) deliberately stay at the flat STATE_DIR root - a
# single board-wide approval queue and a single system pause/offset state
# make more operational sense than fragmenting them per subsidiary, and
# approve.py already reads approval_queue.jsonl directly from STATE_DIR
# without going through this module at all, so moving it would break that
# CLI outright. approval_queue.jsonl records still carry a subsidiary_id
# field (stamped on write) so entries stay traceable/filterable even though
# the file itself isn't physically split.
# --------------------------------------------------------------------------

_MIGRATED_LEGACY_FILES: set = set()


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _subsidiary_dir(subsidiary_id: str = None) -> Path:
    return STATE_DIR / (subsidiary_id or _active_subsidiary_id)


def _migrate_legacy_file_if_needed(filename: str) -> None:
    """One-time backfill (per process, per subsidiary+filename) for files
    written before subsidiary-scoping existed: STATE_DIR/<file> moves to
    STATE_DIR/<subsidiary_id>/<file>, with subsidiary_id stamped onto every
    record that doesn't already have one. The legacy flat file is left in
    place (never deleted) as a safety net - only the new per-subsidiary
    copy is ever read/written going forward, so the old file just becomes
    inert once migrated. Never raises; a failed migration just means the
    subsidiary starts that file fresh rather than blocking the cycle.
    """
    key = (_active_subsidiary_id, filename)
    if key in _MIGRATED_LEGACY_FILES:
        return
    _MIGRATED_LEGACY_FILES.add(key)
    try:
        new_dir = _subsidiary_dir()
        if (new_dir / filename).exists():
            return
        legacy_path = STATE_DIR / filename
        if not legacy_path.exists():
            return
        records = read_jsonl(STATE_DIR, filename)
        if not records:
            return
        for record in records:
            record.setdefault("subsidiary_id", _active_subsidiary_id)
        write_jsonl(new_dir, filename, records)
        print(
            f"[api-sentinel] migrated {filename} ({len(records)} records) to "
            f"per-subsidiary path for '{_active_subsidiary_id}'"
        )
    except OSError as exc:
        print(f"[api-sentinel] WARNING: migration of {filename} failed ({exc}) - starting fresh")


def _append_jsonl(filename: str, record: dict) -> None:
    _migrate_legacy_file_if_needed(filename)
    record.setdefault("subsidiary_id", _active_subsidiary_id)
    append_jsonl(_subsidiary_dir(), filename, record)


def _read_jsonl(filename: str) -> list:
    _migrate_legacy_file_if_needed(filename)
    if filename == "hypotheses.jsonl":
        _backfill_missing_evidence_stage_if_needed()
    return read_jsonl(_subsidiary_dir(), filename)


def _write_jsonl(filename: str, records: list) -> None:
    for record in records:
        record.setdefault("subsidiary_id", _active_subsidiary_id)
    write_jsonl(_subsidiary_dir(), filename, records)


def _append_global_jsonl(filename: str, record: dict) -> None:
    record.setdefault("subsidiary_id", _active_subsidiary_id)
    append_jsonl(STATE_DIR, filename, record)


def _read_global_jsonl(filename: str) -> list:
    return read_jsonl(STATE_DIR, filename)


def _write_global_jsonl(filename: str, records: list) -> None:
    write_jsonl(STATE_DIR, filename, records)


# --------------------------------------------------------------------------
# Evidence-stage artifact gates (structural-rebuild addendum, sections 3-4).
# Crossing into community_engagement or landing_page/build requires a real,
# retrievable artifact - not a self-reported claim that the stage was "done".
# Reads stage_skip_requests.jsonl directly from _holding/ (same reasoning as
# _read_own_policies above: importing holding.py back would be circular).
# --------------------------------------------------------------------------

def _has_substantive_research_artifact(hypothesis_id: str) -> bool:
    return any(
        len((f.get("summary") or "").strip()) >= RESEARCH_FINDING_MIN_LENGTH
        for f in _read_jsonl("research_findings.jsonl")
        if f.get("hypothesis_id") == hypothesis_id
    )


def _has_real_community_engagement_artifact(hypothesis_id: str) -> bool:
    for d in _read_jsonl("content_drafts.jsonl"):
        if d.get("hypothesis_id") != hypothesis_id:
            continue
        if d.get("post_type") not in _COMMUNITY_ENGAGEMENT_POST_TYPES:
            continue
        if d.get("status") == "posted" or d.get("approved_request_id"):
            return True
    return False


def _has_approved_stage_skip(hypothesis_id: str, target_stage: str) -> bool:
    requests = read_jsonl(STATE_DIR / "_holding", "stage_skip_requests.jsonl")
    return any(
        r.get("hypothesis_id") == hypothesis_id
        and r.get("target_stage") == target_stage
        and r.get("status") == "approved"
        for r in requests
    )


# --------------------------------------------------------------------------
# evidence_stage backfill for pre-existing hypotheses (2026-08-11 fix).
# evidence_stage became a required field in the structural-rebuild
# addendum, but that only enforces it going forward on write_hypothesis -
# a hypothesis written before the field existed (hyp_bootstrap_001) just
# has it missing/None on the record itself, which the rest of the gating
# logic (EVIDENCE_STAGES membership checks throughout this file) isn't
# designed to handle. Same one-time-per-process pattern as
# _migrate_legacy_file_if_needed above: infer a real stage from actual
# signals already on/around the record - never a blind guess - and
# persist it, rather than leaving it unset indefinitely.
# --------------------------------------------------------------------------

_MIGRATED_EVIDENCE_STAGE: set = set()


def _infer_legacy_evidence_stage(hypothesis: dict) -> str:
    """Ground-truth inference, not a guess: a hypothesis with real
    economics already computed (estimated_build_cost + build_cost_
    reasoning) or a live landing page was clearly already operating past
    community_engagement under the old, pre-evidence-stage flat model -
    exactly hyp_bootstrap_001's actual history. Otherwise fall back
    through whatever real artifacts exist, and 'research' (the earliest
    stage, unlocking no later-stage gate it hasn't earned) if none do.
    """
    if hypothesis.get("landing_page_live") or (
        hypothesis.get("estimated_build_cost") is not None
        and (hypothesis.get("build_cost_reasoning") or "").strip()
    ):
        return "landing_page"
    if _has_real_community_engagement_artifact(hypothesis.get("id")):
        return "community_engagement"
    return "research"


def _backfill_missing_evidence_stage_if_needed() -> None:
    key = _active_subsidiary_id
    if key in _MIGRATED_EVIDENCE_STAGE:
        return
    _MIGRATED_EVIDENCE_STAGE.add(key)
    hyps = read_jsonl(_subsidiary_dir(), "hypotheses.jsonl")
    changed = False
    for h in hyps:
        if h.get("evidence_stage") in EVIDENCE_STAGES:
            continue
        h["evidence_stage"] = _infer_legacy_evidence_stage(h)
        changed = True
    if changed:
        write_jsonl(_subsidiary_dir(), "hypotheses.jsonl", hyps)
        print(
            f"[api-sentinel] backfilled evidence_stage on legacy hypothesis "
            f"record(s) for '{_active_subsidiary_id}'"
        )


# --------------------------------------------------------------------------
# Anti-instruction-copying tripwire (structural-rebuild addendum, section 5).
# hyp_bootstrap_001's build_cost_reasoning was found to be a near-verbatim
# copy of this module's own instruction text, not independently derived
# reasoning - disqualifying on its own, regardless of what number it
# attached to (see README chapter 15). Plain substring match against known
# template/incident phrasing pulled directly from this file's own docstrings
# - not sophisticated, just enough to catch verbatim/near-verbatim reuse
# rather than genuine hypothesis-specific reasoning.
# --------------------------------------------------------------------------

_INSTRUCTION_ECHO_PHRASES = [
    "old-economy market-rate thinking",
    "agency/freelancer/employee",
    "human developer/agency/employee",
    "typically costs this system a few dollars in tokens",
    "hundreds or thousands of dollars",
    "low single-digit dollars in tokens",
    "genuinely more files/integration points/iteration passes",
    "it feels like it should cost more",
    "a plausible price for a human dev team",
]


def _instruction_echo_match(text: str) -> str:
    """Returns the matched phrase if `text` echoes known instruction/incident
    template language, else "".
    """
    lowered = (text or "").lower()
    for phrase in _INSTRUCTION_ECHO_PHRASES:
        if phrase in lowered:
            return phrase
    return ""


# --------------------------------------------------------------------------
# Signup capture (GitHub Issues, see index.html submitForm())
# --------------------------------------------------------------------------

_SIGNUP_FIELD_RE = re.compile(r"^(Email|Tier|Consent|Timestamp|Variant):\s*(.*)$", re.MULTILINE)


def _parse_signup_body(body: str) -> dict:
    return {key.lower(): value.strip() for key, value in _SIGNUP_FIELD_RE.findall(body or "")}


def sync_signups_from_github() -> int:
    """Fetch issues titled with the signup prefix from the public repo and
    append any not already recorded to signups.jsonl. Uses the unauthenticated
    GitHub Search API (60 req/hour, plenty for a 6h cron); set GITHUB_TOKEN to
    raise the rate limit if it ever becomes a problem. Returns how many new
    signups were recorded.
    """
    known_issue_numbers = {r.get("issue_number") for r in _read_jsonl("signups.jsonl")}

    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    query = f'repo:{GITHUB_REPO} in:title "{SIGNUP_TITLE_PREFIX}" is:issue'
    resp = requests.get(
        f"{GITHUB_API}/search/issues",
        params={"q": query, "per_page": 100},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])

    new_count = 0
    for issue in items:
        number = issue.get("number")
        if number in known_issue_numbers:
            continue
        fields = _parse_signup_body(issue.get("body"))
        _append_jsonl("signups.jsonl", {
            "issue_number": number,
            "email": fields.get("email"),
            "tier": fields.get("tier"),
            "consent": fields.get("consent"),
            "landing_page_variant_id": fields.get("variant"),
            "submitted_at": fields.get("timestamp"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source": "github_issue",
            "url": issue.get("html_url"),
        })
        new_count += 1
    return new_count


# --------------------------------------------------------------------------
# Cross-cycle continuity + usage tracking (orchestration-level, called
# directly from crew.py, not agent tools - the CEO shouldn't be deciding
# whether to persist its own continuity note or usage numbers)
# --------------------------------------------------------------------------

def save_cycle_note(text: str) -> None:
    """Persist a short digest of what happened this cycle so the next
    cycle's channel-strategy task can start with real continuity instead of
    a blank slate every 2h. Overwrites - only the latest cycle's note is
    kept. Per-subsidiary (structural-rebuild addendum, section 2) - this is
    genuinely subsidiary-specific continuity, unlike system_paused.json/
    telegram_update_offset.txt which stay global.
    """
    dir_ = _subsidiary_dir()
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "last_cycle_note.txt").write_text(text, encoding="utf-8")


def read_last_cycle_note() -> str:
    """Return the active subsidiary's previous cycle note, or "" if there
    isn't one yet (e.g. its very first run, or the file predates this
    feature).
    """
    path = _subsidiary_dir() / "last_cycle_note.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def log_cycle_usage(metrics: dict) -> None:
    """Append this cycle's LLM usage (tokens, request count) to a running
    history file - lets cost anomalies like an unusually high request count
    (e.g. from repeated agent retries) be spotted over time, not just
    reported once and forgotten.
    """
    _append_jsonl("usage_history.jsonl", {
        "at": datetime.now(timezone.utc).isoformat(),
        **metrics,
    })


# --------------------------------------------------------------------------
# CEO tools: state, approvals, hypotheses
# --------------------------------------------------------------------------

@tool("read_state")
def read_state() -> str:
    """Read the current pipeline state as JSON: pending approval requests,
    recorded signups, and hypothesis counts. Syncs new signups from GitHub
    first; if that fails (e.g. no network), falls back to whatever is already
    on disk and reports the failure in signup_source instead of hiding it.
    """
    sync_error = None
    try:
        sync_signups_from_github()
    except Exception as exc:
        sync_error = str(exc)

    approvals = _read_global_jsonl("approval_queue.jsonl")
    signups = _read_jsonl("signups.jsonl")
    hypotheses = _read_jsonl("hypotheses.jsonl")
    pending = [a for a in approvals if a.get("status") == "pending"]
    active_hyps = [h for h in hypotheses if h.get("status") == "active"]

    state = {
        "pending_approvals": len(pending),
        "total_approval_requests": len(approvals),
        "total_signups": len(signups),
        "signup_source": "github_issues" if sync_error is None else f"github_issues (sync failed: {sync_error})",
        "total_hypotheses": len(hypotheses),
        "active_hypotheses": len(active_hyps),
    }
    return json.dumps(state, ensure_ascii=False)


@tool("request_approval")
def request_approval(category: str, proposal: str, reasoning: str) -> str:
    """File a request in the human approval queue instead of acting directly.
    category must be one of: spend, legal, publish, deploy, pricing - required
    whenever an action would cost money, create a legal obligation, or become
    publicly visible. The request is written with status "pending" and sits
    there until a human reviews it via approve.py; this tool never executes
    anything itself.

    Payment-intent test requests use category='spend': when a hypothesis
    would benefit from testing real willingness-to-pay (a pre-order/deposit
    on the landing page) rather than only an interest/email signal, file one
    here describing the price point and what kind of pre-order/deposit is
    wanted - never set up a payment processor/link directly, that stays a
    human-only step (same tier as DNS/contracts/new logins), same as this
    tool for every other category. Once approved, a human replies via
    Telegram ("payment_link: <appr_id> <url>") with the actual link; poll
    check_approval_status(approval_id) for payment_link_url rather than
    assuming it exists just because status='approved'. Put the confirmed URL
    literally in the follow-up file_task_order to Dev, same as any other
    concrete artifact (never a paraphrase).

    category='publish' requires `proposal` to be a JSON string (not free
    prose) matching the rigid template (structural-rebuild addendum,
    section 7) - required keys: platform, target_url, title (or literally
    "kein Titel" if the platform has none), text (verbatim content, exactly
    as it will be posted - never paraphrased or summarized), footer (or
    literally "keiner"), hypothesis_id, evidence_stage, is_experiment
    (bool), success_criterion (concrete and falsifiable - state this even
    when the honest answer is "nein, reine Recherche, kein Erfolgskriterium
    noetig", never omit it). Rendered verbatim in the Telegram notification
    (notify_new_pending_approvals) using these exact structured fields,
    never reflowed into prose.
    """
    if category not in APPROVAL_CATEGORIES:
        return json.dumps({
            "error": f"invalid category '{category}', must be one of {sorted(APPROVAL_CATEGORIES)}"
        })
    if category == "publish":
        try:
            template = json.loads(proposal)
        except json.JSONDecodeError as exc:
            return json.dumps({
                "error": f"category='publish' requires proposal to be a JSON string with the structured "
                         f"template fields ({sorted(PUBLISH_TEMPLATE_FIELDS)}), not free prose: {exc}"
            })
        if not isinstance(template, dict):
            return json.dumps({"error": "category='publish' proposal must be a JSON object, not a list/scalar"})
        missing = PUBLISH_TEMPLATE_FIELDS - template.keys()
        if missing:
            return json.dumps({"error": f"category='publish' proposal missing required template fields: {sorted(missing)}"})
        for _field in PUBLISH_TEMPLATE_FIELDS - {"is_experiment"}:
            if not str(template.get(_field) or "").strip():
                return json.dumps({"error": f"publish template field '{_field}' must not be empty"})
        if not isinstance(template.get("is_experiment"), bool):
            return json.dumps({"error": "publish template field 'is_experiment' must be a boolean (true/false)"})

    record = {
        "id": f"appr_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "proposal": proposal,
        "reasoning": reasoning,
        "status": "pending",
    }
    _append_global_jsonl("approval_queue.jsonl", record)
    return json.dumps({"queued": record["id"]})


@tool("check_approval_status")
def check_approval_status(approval_id: str) -> str:
    """Look up a request_approval entry's real status directly, instead of
    trusting another agent's free-text claim that "this was approved". Used
    by Dev to verify a Build-outcome approval actually exists and is
    status='approved' before opening a PR for it - never take the CEO's
    report's word for that alone.

    payment_link_url is only present once a human has actually provisioned
    one and confirmed it via the Telegram "payment_link: <id> <url>" reply
    (see request_approval's payment-intent-test guidance) - null until then,
    even if status is already 'approved'. This system never creates payment
    infrastructure itself; it only asks a human to hand one back.
    """
    approvals = _read_global_jsonl("approval_queue.jsonl")
    approval = next((a for a in approvals if a.get("id") == approval_id), None)
    if approval is None:
        return json.dumps({"error": f"no approval request with id '{approval_id}'"})
    return json.dumps({
        "id": approval["id"],
        "status": approval.get("status"),
        "category": approval.get("category"),
        "payment_link_url": approval.get("payment_link_url"),
    })


@tool("read_hypotheses")
def read_hypotheses(status: str = "") -> str:
    """Return hypotheses as JSON. Pass status="active" or status="evaluated"
    to filter; an empty string returns all of them.
    """
    hyps = _read_jsonl("hypotheses.jsonl")
    if status:
        hyps = [h for h in hyps if h.get("status") == status]
    return json.dumps(hyps, ensure_ascii=False)


@tool("read_due_hypotheses")
def read_due_hypotheses() -> str:
    """Deterministically compute which active hypotheses are due for
    evaluation right now, instead of computing "has duration_days elapsed"
    yourself from read_hypotheses() output - a hypothesis becomes due the
    moment EITHER of its two time-box mechanisms is hit, whichever comes
    first:
    - duration_days has elapsed since created_at (always set, every
      hypothesis has this), or
    - measured.reach_estimate has already reached this hypothesis's own
      sample_size_trigger, if one was set at creation (optional - lets a
      fast channel that already has a real signal get evaluated without
      waiting out the full window).

    A hypothesis left active with neither ever being hit just never
    appears here - the point is to force every hypothesis through the
    four-way evaluation once its own time-box is reached, not to let one
    quietly run indefinitely because evaluating it never felt urgent.
    """
    now = datetime.now(timezone.utc)
    due = []
    for h in _read_jsonl("hypotheses.jsonl"):
        if h.get("status") != "active":
            continue

        reason = None
        created_at = h.get("created_at", "")
        duration_days = h.get("duration_days")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created = None
        if created is not None and duration_days is not None and now >= created + timedelta(days=duration_days):
            reason = "duration_elapsed"

        trigger = h.get("sample_size_trigger")
        reach = (h.get("measured") or {}).get("reach_estimate")
        if reason is None and trigger is not None and reach is not None and reach >= trigger:
            reason = "sample_size_trigger_met"

        if reason:
            due.append({
                "id": h.get("id"), "reason": reason, "created_at": created_at,
                "duration_days": duration_days, "sample_size_trigger": trigger, "reach_estimate": reach,
            })
    return json.dumps(due, ensure_ascii=False)


@tool("write_hypothesis")
def write_hypothesis(hypothesis: str) -> str:
    """Create or update a hypothesis. `hypothesis` must be a JSON string of a
    (possibly partial, if updating an existing one) hypothesis object with at
    least an "id". Creating a new active hypothesis is rejected if its
    landing_page_variant_id already has 2 other active hypotheses (section
    5.6 parallelism rule) - pick a different variant or wait instead.

    Fields required now depend on evidence_stage, not a single flat
    checklist (structural-rebuild addendum, section 2 - Bezos two-way/
    one-way-door framing). Every hypothesis always requires
    hypothesis_type ("value" or "growth"), impact_score/confidence_score
    (your own honest judgment, same shape as a channel's), and
    evidence_stage itself (one of EVIDENCE_STAGES: research,
    community_engagement, landing_page, build - no longer optional, every
    hypothesis declares where it actually is).

    At research/community_engagement (two-way doors - cheap, fast,
    reversible): estimated_build_cost/price_point_monthly/break_even_
    horizon_months/break_even_users/build_cost_reasoning are NOT required
    yet. Use the optional rough_economics_note free-text field for an
    order-of-magnitude planning guess instead (e.g. "probably EUR15-50/mo
    depending on what we learn - not yet computed") - clearly separate from
    the real, load-bearing economics used later. compute_break_even()
    refuses to compute at these stages for the same reason - don't dress up
    a placeholder guess as a precise number.

    evidence_stage='research' additionally requires the research plan
    fields (section 3, logged before research starts, not reconstructed
    afterward): research_objective (the one specific question this is
    meant to answer), research_confirming_criteria and research_
    disconfirming_criteria (concrete and falsifiable). evidence_stage='
    community_engagement' requires a real posted (or approved-and-queued)
    thread_reply/own_question_post draft for this hypothesis first
    (draft_content) - a claim the stage was "done" isn't enough.

    At landing_page/build (crossing toward a one-way door - real cost, real
    commitment): estimated_build_cost, price_point_monthly, break_even_
    horizon_months, break_even_users, and build_cost_reasoning become
    required and must be precise, evidence-grounded numbers. Crossing into
    either for the first time also requires artifact-backed history through
    research (a substantive log_research_finding entry, RESEARCH_FINDING_
    MIN_LENGTH+ chars) AND community_engagement (a real draft_content
    artifact) - or a Main-CEO-approved stage-skip request
    (file_stage_skip_request/holding.py) if skipping genuinely applies.
    This is a real gate, not a self-written excuse - it's what would have
    caught the bootstrap hypothesis skipping straight to a landing page.

    estimated_build_cost MUST be grounded in what this system actually
    pays to build something - Dev-agent LLM calls plus any genuine
    recurring infra cost (hosting, domain), never what a human
    developer/agency/employee would charge. A landing page, signup form,
    or small backend script/webhook realistically costs low single-digit
    dollars in tokens here, not hundreds or thousands. build_cost_reasoning
    is required whenever estimated_build_cost is set (create or update) -
    break the number down into its real components, specific to THIS
    hypothesis's own gathered evidence - never reused/paraphrased from
    example wording in instructions or past incident write-ups (rejected
    mechanically if it echoes known template phrasing, section 5). Above
    SIMPLE_BUILD_COST_CEILING (currently 10.0), build_cost_reasoning must
    also be substantive (at least BUILD_COST_JUSTIFICATION_MIN_LENGTH
    characters) - genuinely more files/integration points/iteration passes
    is a valid reason to go higher, "it feels like it should cost more" is
    not. break_even_horizon_months longer than 1 month also needs
    build_cost_reasoning explaining why (e.g. real recurring infra cost,
    not just habit) - default to 1 unless there's a concrete reason, this
    system is meant to pay for itself fast given how cheap builds are here.

    A time-box is mandatory: duration_days is always required. You may
    additionally set sample_size_trigger (a measured.reach_estimate value
    at which this hypothesis becomes due for evaluation early, before
    duration_days elapses, for a fast channel that doesn't need the full
    window to produce a real signal) - optional, defaults to none. Use
    read_due_hypotheses() to see what's actually due, rather than computing
    elapsed time yourself.

    One-variable-at-a-time applies to every new hypothesis, not just
    pivots: if prior_hypothesis_id points at a hypothesis whose outcome was
    "pivot", this new one must say pivot_variable_changed (one of audience/
    price/copy/channel/timing) and pivot_reasoning - exactly one identified
    variable, logged. If there's no prior_hypothesis_id (a first attempt -
    including the very first hypothesis ever), primary_variable_tested is
    required instead (same five values) - name the one untested assumption
    this test is actually isolating; note what else you're holding constant
    in the optional holding_constant_notes. Never bundle more than one
    genuinely untested variable (e.g. a new audience AND a new price at
    once) into a single first test - you won't be able to tell which part
    of the result to credit.

    Creating a new active hypothesis is rejected if this subsidiary already
    has MAX_ACTIVE_HYPOTHESES hypotheses with status="active" (same
    capping logic as the channel-testing cap - prioritize via
    impact_score/confidence_score instead of running everything at once),
    or if its landing_page_variant_id already has 2 other active
    hypotheses (parallelism rule) - pick a different variant or wait
    instead.

    Setting status="buried" requires a non-empty bury_reasoning - buried
    hypotheses are never deleted, only marked, so the reasoning has to be
    on the record for whoever revisits it later.

    Optional payment_intent_approval_id links this hypothesis to a
    request_approval(category='spend') entry once a real payment-intent
    (pre-order/deposit) test has been requested for it - see task_ceo
    guidance for when that's warranted.
    """
    try:
        patch = json.loads(hypothesis)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    if "id" not in patch:
        return json.dumps({"error": "hypothesis must include an 'id'"})

    if "status" in patch and patch["status"] not in HYPOTHESIS_STATUSES:
        return json.dumps({
            "error": f"invalid status '{patch['status']}', must be one of {sorted(HYPOTHESIS_STATUSES)}"
        })
    if "hypothesis_type" in patch and patch["hypothesis_type"] not in HYPOTHESIS_TYPES:
        return json.dumps({
            "error": f"invalid hypothesis_type '{patch['hypothesis_type']}', must be one of {sorted(HYPOTHESIS_TYPES)}"
        })
    if "outcome" in patch and patch["outcome"] is not None and patch["outcome"] not in scoring.HYPOTHESIS_OUTCOMES:
        return json.dumps({
            "error": f"invalid outcome '{patch['outcome']}', must be one of {sorted(scoring.HYPOTHESIS_OUTCOMES)} or null"
        })
    if "evidence_stage" in patch and patch["evidence_stage"] is not None and patch["evidence_stage"] not in EVIDENCE_STAGES:
        return json.dumps({
            "error": f"invalid evidence_stage '{patch['evidence_stage']}', must be one of {EVIDENCE_STAGES} or null"
        })
    if patch.get("status") == "buried" and not (patch.get("bury_reasoning") or "").strip():
        return json.dumps({"error": "status='buried' requires a non-empty bury_reasoning"})

    hyps = _read_jsonl("hypotheses.jsonl")
    existing_index = next((i for i, h in enumerate(hyps) if h.get("id") == patch["id"]), None)
    existing_record = hyps[existing_index] if existing_index is not None else {}
    prior_stage = existing_record.get("evidence_stage")
    effective_stage = patch.get("evidence_stage", prior_stage)

    # Section 5: reasoning fields must be this hypothesis's own, not reused
    # instruction/incident-template phrasing - checked before anything else
    # touches these fields, so a copied field can't slip through via some
    # other code path first.
    for _echo_field in ("build_cost_reasoning", "defensibility_notes"):
        if _echo_field in patch:
            _echoed = _instruction_echo_match(patch[_echo_field])
            if _echoed:
                return json.dumps({
                    "error": f"{_echo_field} echoes known instruction/incident template language "
                             f"('{_echoed}') rather than this hypothesis's own reasoning - describe "
                             "what's actually specific to this hypothesis, don't reuse example wording"
                })

    # Section 3: research plan required before evidence_stage='research'
    # actually takes effect - the objective and confirming/disconfirming
    # criteria, logged before research starts.
    if effective_stage == "research" and prior_stage != "research":
        _merged_plan = {**existing_record, **patch}
        _missing_plan = {f for f in RESEARCH_PLAN_FIELDS if not (_merged_plan.get(f) or "").strip()}
        if _missing_plan:
            return json.dumps({
                "error": f"evidence_stage='research' requires the research plan fields first: "
                         f"{sorted(_missing_plan)} - the specific question this research answers, and "
                         "what counts as confirming vs. disconfirming evidence, logged before research starts"
            })

    # Section 4: crossing into community_engagement needs a real artifact,
    # not just the claim the stage was "done".
    if effective_stage == "community_engagement" and prior_stage not in ("community_engagement", *EVIDENCE_LATER_STAGES):
        if not _has_real_community_engagement_artifact(patch["id"]) and not _has_approved_stage_skip(patch["id"], "community_engagement"):
            return json.dumps({
                "error": "evidence_stage='community_engagement' requires a real posted (or "
                         "approved-and-queued) thread_reply/own_question_post draft for this hypothesis "
                         "first (draft_content) - or a Main-CEO-approved stage-skip request "
                         "(file_stage_skip_request) if this genuinely doesn't apply here"
            })

    # Section 4: crossing into landing_page/build for the first time needs
    # artifact-backed research AND community_engagement history, or a
    # Main-CEO-reviewed stage-skip request - never a self-written excuse.
    if effective_stage in EVIDENCE_LATER_STAGES and prior_stage not in EVIDENCE_LATER_STAGES:
        _has_research = _has_substantive_research_artifact(patch["id"])
        _has_engagement = _has_real_community_engagement_artifact(patch["id"])
        if not (_has_research and _has_engagement) and not _has_approved_stage_skip(patch["id"], effective_stage):
            _missing_bits = []
            if not _has_research:
                _missing_bits.append("a substantive research_findings.jsonl entry (log_research_finding)")
            if not _has_engagement:
                _missing_bits.append("a real posted/queued community_engagement draft (draft_content)")
            return json.dumps({
                "error": f"evidence_stage='{effective_stage}' requires artifact-backed history first - "
                         f"missing: {'; '.join(_missing_bits)}. If skipping genuinely applies here (e.g. "
                         "research truly isn't relevant), file_stage_skip_request for the Main-CEO to "
                         "review instead of setting this directly."
            })

    # AI-native economics addendum: whenever estimated_build_cost is being
    # set (create or update), it must be justified - and justified more
    # substantively the higher it goes above what a Dev-agent LLM build
    # actually costs. Falls back to the existing record's own reasoning on
    # an update that doesn't re-touch build_cost_reasoning, so a legitimate
    # unrelated update isn't blocked by this.
    effective_reasoning = (
        patch["build_cost_reasoning"] if "build_cost_reasoning" in patch
        else existing_record.get("build_cost_reasoning")
    )
    effective_reasoning = (effective_reasoning or "").strip()
    if "estimated_build_cost" in patch:
        if not effective_reasoning:
            return json.dumps({
                "error": "estimated_build_cost requires a non-empty build_cost_reasoning - ground it in "
                         "what this system actually pays (Dev-agent LLM calls + genuine recurring infra "
                         "cost), never a human developer/agency/employee rate"
            })
        cost = patch["estimated_build_cost"]
        if cost > SIMPLE_BUILD_COST_CEILING and len(effective_reasoning) < BUILD_COST_JUSTIFICATION_MIN_LENGTH:
            return json.dumps({
                "error": f"estimated_build_cost of {cost} exceeds the ${SIMPLE_BUILD_COST_CEILING:.0f} "
                         "sanity-check ceiling for a typical Dev-buildable artifact (landing page, signup "
                         "form, small backend script/webhook) - token cost alone should land in the very "
                         "low single digits here. Going higher needs a substantive build_cost_reasoning "
                         f"(at least {BUILD_COST_JUSTIFICATION_MIN_LENGTH} chars) citing genuine additional "
                         "token/iteration volume (more files, more integration points, more passes needed) "
                         "- not 'it feels like it should cost more'"
            })
    effective_horizon = patch.get("break_even_horizon_months", existing_record.get("break_even_horizon_months"))
    if effective_horizon is not None and effective_horizon > 1 and not effective_reasoning:
        return json.dumps({
            "error": "break_even_horizon_months > 1 requires a non-empty build_cost_reasoning explaining "
                     "why (e.g. real recurring infra cost) - default to 1 month unless there's a concrete "
                     "reason, builds are cheap enough here that a validated idea should pay for itself fast"
        })

    # Section 2: economics only load-bearing (required) once the final
    # state is landing_page/build - a one-way door with real cost/
    # commitment. Checked against the full merged state, not just this
    # patch's own keys, so economics set on an earlier update aren't
    # demanded again on an unrelated later one.
    if effective_stage in EVIDENCE_LATER_STAGES:
        _merged_econ = {**existing_record, **patch}
        _missing_econ = {f for f in STAGE_GATED_ECONOMICS_FIELDS if _merged_econ.get(f) is None}
        if _missing_econ:
            return json.dumps({
                "error": f"evidence_stage='{effective_stage}' requires real economics now: "
                         f"{sorted(_missing_econ)} - this is a one-way door, the numbers backing it must "
                         "be precise and evidence-grounded, not a rough_economics_note guess anymore"
            })

    # Section 6: duration-cap policy, board-set via Telegram confirmation
    # (see DEFAULT_PROPOSED_DURATION_CAPS) - only enforced once
    # status=='confirmed', never a silently-active default. Anything over
    # the confirmed ceiling for this stage still goes through the existing
    # approval queue (duration_extension_approval_id pointing at an
    # approved request_approval) - the escalation path itself doesn't
    # change, only the source of the ceiling number does.
    effective_duration = patch.get("duration_days", existing_record.get("duration_days"))
    if effective_duration is not None and effective_stage:
        _duration_policy = _read_own_policies().get("max_duration_days_by_stage") or {}
        if _duration_policy.get("status") == "confirmed":
            _ceiling = (_duration_policy.get("values") or {}).get(effective_stage)
            if _ceiling is not None and effective_duration > _ceiling:
                _ext_approval_id = patch.get(
                    "duration_extension_approval_id", existing_record.get("duration_extension_approval_id")
                )
                _ext_approval = None
                if _ext_approval_id:
                    _ext_approval = next(
                        (a for a in _read_global_jsonl("approval_queue.jsonl") if a.get("id") == _ext_approval_id), None
                    )
                if not _ext_approval or _ext_approval.get("status") != "approved":
                    return json.dumps({
                        "error": f"duration_days={effective_duration} exceeds the confirmed policy ceiling "
                                 f"({_ceiling} days) for evidence_stage='{effective_stage}' - shorten it, or "
                                 "pass duration_extension_approval_id pointing at an approved request_approval"
                    })

    if existing_index is None:
        missing = BASE_REQUIRED_HYPOTHESIS_FIELDS - patch.keys()
        if missing:
            return json.dumps({"error": f"new hypothesis missing required fields: {sorted(missing)}"})
        prior_id = patch.get("prior_hypothesis_id")
        if prior_id:
            prior = next((h for h in hyps if h.get("id") == prior_id), None)
            if prior is not None and prior.get("outcome") == "pivot":
                if patch.get("pivot_variable_changed") not in PIVOT_VARIABLES:
                    return json.dumps({
                        "error": "this hypothesis follows a 'pivot' outcome - pivot_variable_changed "
                                 f"must be one of {sorted(PIVOT_VARIABLES)} (exactly one identified variable)"
                    })
                if not (patch.get("pivot_reasoning") or "").strip():
                    return json.dumps({"error": "this hypothesis follows a 'pivot' outcome - pivot_reasoning is required"})
        else:
            # First attempt (including the very first hypothesis ever) -
            # the one-variable rule applies here too, not just to pivots
            # (four-fixes addendum, point 2). No prior to diff against, so
            # this is a self-declared "what am I actually isolating" field
            # rather than something mechanically diffable, same trust model
            # as pivot_variable_changed above.
            if patch.get("primary_variable_tested") not in PIVOT_VARIABLES:
                return json.dumps({
                    "error": "a first-attempt hypothesis (no prior_hypothesis_id) requires "
                             f"primary_variable_tested, one of {sorted(PIVOT_VARIABLES)} - name the one "
                             "untested assumption this test isolates; don't bundle more than one "
                             "genuinely untested variable into a single first test"
                })
        if patch.get("status", "active") == "active":
            active_count = sum(1 for h in hyps if h.get("status") == "active")
            if active_count >= MAX_ACTIVE_HYPOTHESES:
                return json.dumps({
                    "error": f"{active_count} hypotheses are already status='active' "
                             f"(max {MAX_ACTIVE_HYPOTHESES}) - rank candidate ideas by impact_score/"
                             "confidence_score and the economics/defensibility/channel-fit reasoning "
                             "already captured per hypothesis, same Bullseye logic as the channel cap, "
                             "and only write the highest-priority one(s); move a hypothesis to "
                             "'evaluated'/'buried' first to free capacity"
                })
            channel_record = next(
                (c for c in _read_jsonl("channels.jsonl") if c.get("id") == patch["channel"]), None
            )
            if channel_record is None or channel_record.get("status") != "testing":
                return json.dumps({
                    "error": f"channel '{patch['channel']}' is not currently status='testing' in the "
                             "roster - call read_channels()/write_channel() to run the Bullseye "
                             "channel-selection step and promote it first"
                })
            active_same_variant = [
                h for h in hyps
                if h.get("status") == "active" and h.get("landing_page_variant_id") == patch["landing_page_variant_id"]
            ]
            if len(active_same_variant) >= 2:
                return json.dumps({
                    "error": f"parallelism limit reached for variant {patch['landing_page_variant_id']} "
                             f"({len(active_same_variant)} active already, max 2)"
                })
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "interim_proxy": True,
            "measured": {"conversions": 0, "reach_estimate": None, "reach_source": None},
            "score": None,
            "outcome": None,
            "prior_hypothesis_id": None,
            "prior_score": None,
            "extension_used": False,
            "next_step": None,
            "landing_page_live": False,
            "defensibility_notes": None,
            "pricing_tier_reasoning": None,
            "expansion_notes": None,
            "channel_fit_reasoning": None,
            "sample_size_trigger": None,
            "primary_variable_tested": None,
            "holding_constant_notes": None,
            "evidence_stage": None,
            "payment_intent_approval_id": None,
            # Economics: no longer unconditionally required at create time
            # (section 2) - explicit None defaults so the keys are always
            # present for a reader, even at research/community_engagement
            # where they're genuinely not known yet.
            "estimated_build_cost": None,
            "price_point_monthly": None,
            "break_even_horizon_months": None,
            "break_even_users": None,
            "build_cost_reasoning": None,
            "rough_economics_note": None,
            "research_objective": None,
            "research_confirming_criteria": None,
            "research_disconfirming_criteria": None,
            **patch,
        }
        hyps.append(record)
    else:
        merged = dict(hyps[existing_index])
        for key, value in patch.items():
            if key == "measured" and isinstance(value, dict) and isinstance(merged.get("measured"), dict):
                merged["measured"] = {**merged["measured"], **value}
            else:
                merged[key] = value
        hyps[existing_index] = merged

    _write_jsonl("hypotheses.jsonl", hyps)

    # 2026-08-11 fix: closing orphaned task orders tied to a buried
    # hypothesis used to depend entirely on an LLM instruction (task_ceo's
    # bury step telling the Sub-CEO to call complete_task_order per open
    # order) - which was never actually reliable (ceo_agent doesn't even
    # have complete_task_order in its tool list, so that instruction was
    # unexecutable as written) and would have needed to be followed
    # correctly on every future bury, not just this one. Now mechanical:
    # burying a hypothesis always closes its own open orders as part of
    # the same write, regardless of which agent/instruction triggered it.
    final_status = (record if existing_index is None else merged).get("status")
    orders_auto_closed = 0
    if final_status == "buried":
        orders = _read_jsonl("task_orders.jsonl")
        for o in orders:
            if o.get("hypothesis_id") == patch["id"] and o.get("status") == "open":
                o["status"] = "done"
                o["result"] = f"auto-cancelled - hypothesis '{patch['id']}' was buried"
                o["completed_at"] = datetime.now(timezone.utc).isoformat()
                orders_auto_closed += 1
        if orders_auto_closed:
            _write_jsonl("task_orders.jsonl", orders)

    response = {"ok": True, "id": patch["id"]}
    if orders_auto_closed:
        response["orders_auto_closed"] = orders_auto_closed
    return json.dumps(response)


def _count_pivot_attempts(hyps_by_id: dict, hypothesis_id: str) -> int:
    """Walk the full prior_hypothesis_id chain (unbounded, unlike
    check_escalation's last-3 window - the pivot cap needs the true total)
    counting how many ancestors were themselves a 'pivot' outcome.
    """
    count = 0
    current = hyps_by_id.get(hypothesis_id)
    seen = set()
    while current and current.get("id") not in seen:
        seen.add(current.get("id"))
        if current.get("outcome") == "pivot":
            count += 1
        current = hyps_by_id.get(current.get("prior_hypothesis_id"))
    return count


@tool("evaluate_hypothesis")
def evaluate_hypothesis(hypothesis_id: str) -> str:
    """Compute the real score, verdict, and four-way outcome for a
    hypothesis (section 5.3/5.4 plus the outcome-engine addendum). Counts
    conversions from signups.jsonl (matched by landing_page_variant_id and a
    submitted_at timestamp inside [created_at, created_at+duration_days])
    and uses whatever measured.reach_estimate is already stored - it does
    NOT guess a reach number. If reach_estimate is still null, returns an
    error saying so instead of scoring; get Growth to call
    read_channel_metrics and write_hypothesis first.

    outcome is one of:
    - "build": score >= 0.7 AND conversions already clear this hypothesis's
      own break_even_users - a strong rate on too few real conversions is
      "test_further", not "build", even if break_even_users is small.
    - "test_further": ambiguous score, or a strong score without enough
      real conversions yet - fires only once (extension_used gate), same
      as the existing single-extension mechanic.
    - "pivot": weak-negative score (or an ambiguous score that already used
      its one extension), and this hypothesis's lineage hasn't spent its
      pivot budget (PIVOT_ATTEMPT_CAP) yet.
    - "bury": clearly bad score, or the pivot budget for this lineage is
      exhausted. Not permanent - a buried hypothesis can be revisited later,
      it's just not automatically retried by this loop anymore.

    Read-only: does not persist anything itself, call write_hypothesis
    afterwards to save status/score/outcome/measured.
    """
    hyps = _read_jsonl("hypotheses.jsonl")
    hyp = next((h for h in hyps if h.get("id") == hypothesis_id), None)
    if hyp is None:
        return json.dumps({"error": f"no hypothesis with id '{hypothesis_id}'"})

    reach = (hyp.get("measured") or {}).get("reach_estimate")
    if not reach:
        return json.dumps({
            "error": "measured.reach_estimate is not set yet - call read_channel_metrics "
                     "and write_hypothesis to record it before evaluating"
        })

    break_even_users = hyp.get("break_even_users")
    if not break_even_users:
        return json.dumps({
            "error": "break_even_users is not set on this hypothesis - it must be computed via "
                     "compute_break_even() and written at creation time, before the experiment runs"
        })

    created_at = hyp.get("created_at", "")
    duration_days = hyp.get("duration_days")
    try:
        window_start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return json.dumps({"error": f"hypothesis has an unparseable created_at: '{created_at}'"})
    window_end = window_start if duration_days is None else window_start + timedelta(days=duration_days)

    variant_id = hyp.get("landing_page_variant_id")
    conversions = 0
    for s in _read_jsonl("signups.jsonl"):
        if s.get("landing_page_variant_id") != variant_id:
            continue
        ts = s.get("submitted_at")
        if not ts:
            continue
        try:
            submitted = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if window_start <= submitted <= window_end:
            conversions += 1

    try:
        score = scoring.compute_score(conversions, reach, hyp["failure_rate"], hyp["success_rate"])
    except (KeyError, ValueError) as exc:
        return json.dumps({"error": str(exc)})

    hyps_by_id = {h["id"]: h for h in hyps if "id" in h}
    pivot_attempts = _count_pivot_attempts(hyps_by_id, hypothesis_id)
    outcome = scoring.classify_outcome(
        score, conversions, break_even_users, bool(hyp.get("extension_used")), pivot_attempts,
    )

    return json.dumps({
        "hypothesis_id": hypothesis_id,
        "conversions": conversions,
        "estimated_reach": reach,
        "score": score,
        "verdict": scoring.verdict_for_score(score),
        "outcome": outcome,
        "break_even_users": break_even_users,
        "pivot_attempts_so_far": pivot_attempts,
    })


@tool("check_escalation")
def check_escalation(hypothesis_id: str) -> str:
    """Walk the prior_hypothesis_id chain back from this (just-evaluated)
    hypothesis and collect up to the last 3 evaluated scores in that lineage
    (this hypothesis plus its ancestors). Returns escalate=true if there are
    at least 3 such scores and their rolling average is <= -0.5 (section
    5.5) - if so, file a request_approval flagging a fundamental strategy
    change instead of continuing to pivot in small steps.
    """
    hyps = {h["id"]: h for h in _read_jsonl("hypotheses.jsonl") if "id" in h}
    chain_scores = []
    current = hyps.get(hypothesis_id)
    while current and len(chain_scores) < 3:
        if current.get("score") is not None:
            chain_scores.append(current["score"])
        current = hyps.get(current.get("prior_hypothesis_id"))

    if not chain_scores:
        return json.dumps({"escalate": False, "reason": "no evaluated scores in lineage yet"})

    avg = sum(chain_scores) / len(chain_scores)
    escalate = len(chain_scores) >= 3 and avg <= -0.5
    return json.dumps({"escalate": escalate, "rolling_average": round(avg, 3), "scores_used": chain_scores})


@tool("compute_break_even")
def compute_break_even(
    estimated_build_cost: float, price_point_monthly: float, break_even_horizon_months: float,
    evidence_stage: str = "",
) -> str:
    """Compute break_even_users for a hypothesis before it's created - how
    many paying users, sustained for break_even_horizon_months at
    price_point_monthly, are needed to recoup estimated_build_cost. Never
    estimate this by hand; this is the same "no mental arithmetic" rule
    evaluate_hypothesis already enforces for scores. The result is required
    on write_hypothesis once evidence_stage reaches landing_page/build and
    is what write_hypothesis's parallel evaluate_hypothesis call later
    checks real conversions against to decide "build" vs "test_further" -
    a low break_even_users makes even a tiny real sample a legitimate build
    basis, a high one means a small positive sample is not enough evidence
    yet, regardless of how good the rate looks.

    Pass evidence_stage to get the section-2 two-way-door check: at
    research/community_engagement this refuses to compute at all
    (returns applicable=False) rather than dressing up a placeholder guess
    as a precise number - use rough_economics_note on write_hypothesis
    instead at those stages. Omit evidence_stage (or pass landing_page/
    build) to compute normally.
    """
    if evidence_stage in EVIDENCE_EARLY_STAGES:
        return json.dumps({
            "applicable": False,
            "reason": f"not yet applicable at evidence_stage='{evidence_stage}' - break_even_users is "
                      "only meaningful once real economics are being locked in (landing_page/build); use "
                      "rough_economics_note on write_hypothesis for an order-of-magnitude guess instead",
        })
    try:
        break_even_users = scoring.compute_break_even_users(
            estimated_build_cost, price_point_monthly, break_even_horizon_months,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"applicable": True, "break_even_users": break_even_users})


# --------------------------------------------------------------------------
# Distilled knowledge base (four-fixes addendum, point 3) - hypotheses.jsonl
# is a log of individual attempts, not accumulated knowledge: without this,
# the system can re-test the same thing in a different wrapper later
# without noticing. Short, consultable takeaways per topic/channel/tactic,
# checked BEFORE generating a new hypothesis - the same cheap-first-step
# spirit as the research-evidence tier, just distilled from this
# subsidiary's own prior hypotheses instead of external sources.
# --------------------------------------------------------------------------

KNOWLEDGE_CONFIDENCE_LEVELS = {"low", "moderate", "high"}


@tool("write_knowledge_entry")
def write_knowledge_entry(
    topic: str, takeaway: str, confidence: str, source_hypothesis_ids: str,
    channel: str = "", tactic: str = "",
) -> str:
    """Distill a short, consultable takeaway into the knowledge base -
    call this whenever a hypothesis resolves to build/pivot/bury (not
    test_further, that's a continuation, not yet a resolved takeaway). A
    pivot's "why it didn't fit" is worth distilling just as much as a
    build's "this worked" - don't only log the wins.

    topic: what this is actually about (e.g. "Reddit organic on
    r/algotrading" or "$5/mo price point for solo devs").
    takeaway: one or two sentences, e.g. "tested 4x, weak below ~50 karma
    accounts, moderate confidence" - short enough to actually get read
    before writing a new hypothesis, not a report.
    confidence: one of low/moderate/high - your own honest read of how
    much this takeaway should be trusted, given how many hypotheses back
    it (a single data point is "low", regardless of how clean the result
    looked).
    source_hypothesis_ids: JSON array string of the hypothesis id(s) this
    takeaway is distilled from, e.g. '["hyp_ab12cd34"]' - always at least
    one, so the takeaway traces back to real evidence.
    channel / tactic: optional, filterable tags (e.g. channel="reddit",
    tactic="own_question_post") when the takeaway is specific to one.
    """
    if not topic.strip():
        return json.dumps({"error": "topic must not be empty"})
    if not takeaway.strip():
        return json.dumps({"error": "takeaway must not be empty"})
    if confidence not in KNOWLEDGE_CONFIDENCE_LEVELS:
        return json.dumps({
            "error": f"invalid confidence '{confidence}', must be one of {sorted(KNOWLEDGE_CONFIDENCE_LEVELS)}"
        })
    try:
        source_ids = json.loads(source_hypothesis_ids)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON for source_hypothesis_ids: {exc}"})
    if not isinstance(source_ids, list) or not source_ids:
        return json.dumps({"error": "source_hypothesis_ids must be a non-empty JSON array of hypothesis ids"})

    record = {
        "id": f"knowledge_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "takeaway": takeaway,
        "confidence": confidence,
        "source_hypothesis_ids": source_ids,
        "channel": channel or None,
        "tactic": tactic or None,
    }
    _append_jsonl("knowledge_base.jsonl", record)
    return json.dumps({"ok": True, "id": record["id"]})


@tool("read_knowledge_base")
def read_knowledge_base(topic: str = "", channel: str = "") -> str:
    """Read distilled takeaways before generating a new hypothesis - check
    whether this topic/channel/tactic has already been tested before
    proposing it again in a different wrapper without noticing. topic
    matches as a case-insensitive substring against each entry's topic;
    channel matches exactly. Both empty returns every entry, most recent
    last.
    """
    entries = _read_jsonl("knowledge_base.jsonl")
    if topic:
        topic_lower = topic.strip().lower()
        entries = [e for e in entries if topic_lower in (e.get("topic") or "").lower()]
    if channel:
        entries = [e for e in entries if e.get("channel") == channel]
    return json.dumps(entries, ensure_ascii=False)


# --------------------------------------------------------------------------
# Structured handoff: Sub-CEO -> executing agent (Growth/Dev). Replaces
# relying on CrewAI's automatic free-text task-output-as-context passing
# for anything that matters - a fixed record instead of paraphrased prose,
# so what was actually asked survives the hop. Lives here (not holding.py)
# because this handoff is entirely within the api-sentinel subsidiary's own
# operative layer, not a holding-level concern.
# --------------------------------------------------------------------------

TASK_ORDER_ROLES = {"growth", "dev"}


@tool("file_task_order")
def file_task_order(to_role: str, task_description: str, context: str, hypothesis_id: str = "") -> str:
    """Sub-CEO hands a concrete task to Growth or Dev as a fixed record,
    instead of leaving it to be inferred from free-text task output.
    to_role must be "growth" or "dev". task_description is the concrete ask
    (e.g. "build landing page variant for hyp_x123, testing a $15/mo price
    point"); context is the why. Pass hypothesis_id whenever this task
    ties back to one - most do.

    Evidence-stage gate (to_role="dev" with a hypothesis_id only): Dev work
    is real cost committed on a hypothesis, so this is rejected unless that
    hypothesis's evidence_stage (read_hypotheses) is already "landing_page"
    or "build". There is no self-written bypass here anymore (structural-
    rebuild addendum, section 4) - a self-signed stage_justification string
    was exactly how the bootstrap hypothesis skipped straight to a landing
    page. The real gate lives on write_hypothesis: getting evidence_stage to
    landing_page/build in the first place already requires artifact-backed
    research/community_engagement history or a Main-CEO-approved stage-skip
    request (file_stage_skip_request) - by the time it's set, it's earned.
    """
    if to_role not in TASK_ORDER_ROLES:
        return json.dumps({"error": f"invalid to_role '{to_role}', must be one of {sorted(TASK_ORDER_ROLES)}"})
    if to_role == "dev" and hypothesis_id:
        hyp = next((h for h in _read_jsonl("hypotheses.jsonl") if h.get("id") == hypothesis_id), None)
        stage = hyp.get("evidence_stage") if hyp else None
        if stage not in EVIDENCE_LATER_STAGES:
            return json.dumps({
                "error": f"hypothesis '{hypothesis_id}' has evidence_stage={stage!r}, not yet "
                         "'landing_page'/'build' - progress it there first via write_hypothesis "
                         "(artifact-backed, or via an approved stage-skip request) before ordering Dev work"
            })
    record = {
        "id": f"order_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "from_role": "sub_ceo",
        "to_role": to_role,
        "hypothesis_id": hypothesis_id or None,
        "task_description": task_description,
        "context": context,
        "status": "open",
        "result": None,
    }
    _append_jsonl("task_orders.jsonl", record)
    return json.dumps({"filed": record["id"]})


@tool("read_task_orders")
def read_task_orders(to_role: str, status: str = "") -> str:
    """Read task orders addressed to a role (growth/dev). Pass status="open"
    for what's actually pending, or "" for all (including already-done ones,
    useful for continuity across cycles).
    """
    orders = [o for o in _read_jsonl("task_orders.jsonl") if o.get("to_role") == to_role]
    if status:
        orders = [o for o in orders if o.get("status") == status]
    return json.dumps(orders, ensure_ascii=False)


@tool("complete_task_order")
def complete_task_order(order_id: str, result: str) -> str:
    """Growth/Dev closes the loop on a task order with a fixed result
    instead of just narrating it in prose - this is what the Sub-CEO (and,
    via status reports, the Main-CEO) actually reads back, not a summary of
    the summary.
    """
    orders = _read_jsonl("task_orders.jsonl")
    idx = next((i for i, o in enumerate(orders) if o.get("id") == order_id), None)
    if idx is None:
        return json.dumps({"error": f"no task order with id '{order_id}'"})
    if orders[idx].get("status") == "done":
        return json.dumps({"error": f"'{order_id}' is already marked done, not overwriting its result"})
    orders[idx]["status"] = "done"
    orders[idx]["result"] = result
    orders[idx]["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_jsonl("task_orders.jsonl", orders)
    return json.dumps({"ok": True, "id": order_id})


# --------------------------------------------------------------------------
# CEO tool: channel-selection roster (Bullseye framework, Traction by
# Weinberg & Mares) - which traction channels to test hypotheses on, decided
# separately from and before which hypothesis to run on a given channel.
# --------------------------------------------------------------------------

@tool("read_channels")
def read_channels(status: str = "") -> str:
    """Return the traction-channel roster as JSON: candidate channels the
    CEO has brainstormed (Bullseye framework), each with impact_score,
    confidence_score, cost_to_test_usd, is_paid, status
    (not_tested/bench/testing/retired), and status_history. Pass a status to
    filter, or "" for all.
    """
    channels = _read_jsonl("channels.jsonl")
    if status:
        channels = [c for c in channels if c.get("status") == status]
    return json.dumps(channels, ensure_ascii=False)


@tool("write_channel")
def write_channel(channel: str, reason: str = "") -> str:
    """Create or update a candidate traction channel (Bullseye framework:
    brainstorm channels, score them, test a small number cheaply in
    parallel, double down on the winner, swap out ones that stop working
    instead of grinding on them).

    `channel` is a JSON string: a full object (id, name, category, is_paid,
    impact_score, confidence_score, and optionally cost_to_test_usd,
    metrics_channel, notes) to create a new candidate, or a partial patch
    (must include "id") to update one. status must be one of
    not_tested/bench/testing/retired.

    Enforced invariants (everything else - which channels to brainstorm,
    how to score them - is your own judgment, not hardcoded here):
    - at most 3 channels may have status="testing" at once. Bullseye's
      central lesson is that startups fail by spreading thin across many
      channels at once, not by picking the "wrong" one first.
    - a paid channel (is_paid=true) cannot move to status="testing" at all
      unless this subsidiary's policies have paid_channels_allowed=true
      (read_subsidiary_policies) - check that before spending time
      brainstorming paid channels. If allowed, it also still needs
      approved_request_id pointing at an approval_queue.jsonl entry with
      status="approved" and category="spend" - this reuses the existing
      human approval queue, it does not add a separate spend gate.
    - changing an existing channel's status requires a non-empty `reason`
      (e.g. "reddit averaging -0.4 over 3 evaluated hypotheses, swapping in
      content_marketing") so swaps stay auditable, same as any other
      decision that changes direction.
    - creating a channel whose name (case/whitespace-insensitive) already
      matches an existing roster entry is rejected, even under a different
      id - update the existing id instead of writing a near-duplicate.
      read_channels() first if unsure whether something's already there.
    """
    try:
        patch = json.loads(channel)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    if "id" not in patch:
        return json.dumps({"error": "channel must include an 'id'"})

    if "status" in patch and patch["status"] not in CHANNEL_STATUSES:
        return json.dumps({
            "error": f"invalid status '{patch['status']}', must be one of {sorted(CHANNEL_STATUSES)}"
        })

    channels = _read_jsonl("channels.jsonl")
    existing_index = next((i for i, c in enumerate(channels) if c.get("id") == patch["id"]), None)
    now = datetime.now(timezone.utc).isoformat()

    if existing_index is None:
        if len(channels) >= MAX_TOTAL_CHANNELS:
            return json.dumps({
                "error": f"roster already has {len(channels)} channels (max {MAX_TOTAL_CHANNELS}) - "
                         "work with the existing candidates (read_channels) instead of brainstorming "
                         "more; if this is a retry after an earlier error, the roster from that "
                         "earlier attempt is very likely already there"
            })
        missing = REQUIRED_CHANNEL_FIELDS - patch.keys()
        if missing:
            return json.dumps({"error": f"new channel missing required fields: {sorted(missing)}"})
        name_norm = patch["name"].strip().casefold()
        name_match = next(
            (c for c in channels if (c.get("name") or "").strip().casefold() == name_norm), None,
        )
        if name_match is not None:
            return json.dumps({
                "error": f"a channel named '{patch['name']}' already exists as id "
                         f"'{name_match.get('id')}' (status={name_match.get('status')}) - call "
                         f"read_channels() and update that id instead of creating a near-duplicate "
                         "under a new one; if you already wrote this channel earlier in this same "
                         "run, this is that same call landing twice, not a new channel"
            })
        old_status = None
        record = {
            "created_at": now,
            "cost_to_test_usd": None,
            "metrics_channel": None,
            "notes": "",
            "approved_request_id": None,
            "status_history": [],
            **patch,
        }
        record["status"] = patch.get("status", "not_tested")
    else:
        old = channels[existing_index]
        old_status = old.get("status")
        if "status" in patch and patch["status"] != old_status and not reason.strip():
            return json.dumps({
                "error": "changing an existing channel's status requires a non-empty reason "
                         "(audit trail for channel swaps)"
            })
        record = {**old, **patch}
        record.setdefault("status", old_status)

    record["updated_at"] = now
    target_status = record["status"]

    if target_status == "testing":
        other_testing = [c for c in channels if c.get("status") == "testing" and c.get("id") != patch["id"]]
        if len(other_testing) >= MAX_CHANNELS_TESTING:
            return json.dumps({
                "error": f"testing cap reached ({len(other_testing)} channels already testing, "
                         f"max {MAX_CHANNELS_TESTING}) - move one to bench/retired first, or hold off"
            })
        if record.get("is_paid"):
            if not _read_own_policies().get("paid_channels_allowed"):
                return json.dumps({
                    "error": "this subsidiary's policies have paid_channels_allowed=false - a paid "
                             "channel cannot move to status='testing' at all right now. This is a "
                             "Main-CEO-level setting (update_subsidiary_policies), not something to "
                             "work around here - use an organic channel instead or escalate via "
                             "file_cross_subsidiary_request if you think the policy should change."
                })
            approved_request_id = record.get("approved_request_id")
            approval = None
            if approved_request_id:
                approvals = _read_global_jsonl("approval_queue.jsonl")
                approval = next((a for a in approvals if a.get("id") == approved_request_id), None)
            if not approval or approval.get("status") != "approved" or approval.get("category") != "spend":
                return json.dumps({
                    "error": "paid channel cannot move to status='testing' without approved_request_id "
                             "pointing at an approved, category='spend' entry in the approval queue - "
                             "file request_approval first, get it approved, then pass its id here"
                })

    if target_status != old_status:
        record["status_history"] = record.get("status_history", []) + [{
            "at": now, "from": old_status, "to": target_status, "reason": reason or "initial",
        }]

    if existing_index is None:
        channels.append(record)
    else:
        channels[existing_index] = record

    _write_jsonl("channels.jsonl", channels)
    return json.dumps({"ok": True, "id": patch["id"], "status": target_status})


# --------------------------------------------------------------------------
# Growth tool: channel metrics, with keyless public auto-fetch where possible
# --------------------------------------------------------------------------

REDDIT_USER_AGENT = "api-sentinel-growth-bot/1.0 (by /u/evolution5s)"
_TELEGRAM_MEMBER_RE = re.compile(r"([\d,\. ]+[KM]?)\s*(?:subscribers|members)", re.IGNORECASE)


def fetch_reddit_public_metrics(post_url: str) -> dict:
    """Fetch upvotes/comments for a public Reddit post - no auth required,
    just a descriptive User-Agent (Reddit blocks generic ones). This is real
    public data, distinct from the author-only "post insights" view count
    that Reddit does not expose via any API.
    """
    url = post_url.rstrip("/") + ".json"
    resp = requests.get(url, headers={"User-Agent": REDDIT_USER_AGENT}, timeout=15)
    resp.raise_for_status()
    post = resp.json()[0]["data"]["children"][0]["data"]
    return {"upvotes": post.get("ups"), "comments": post.get("num_comments"), "upvote_ratio": post.get("upvote_ratio")}


def fetch_discord_public_metrics(invite_code: str) -> dict:
    """Fetch approximate member/online counts for a public Discord server via
    its invite code - no bot token needed, works for any invite link.
    """
    invite_code = invite_code.strip().rstrip("/").split("/")[-1]
    resp = requests.get(
        f"https://discord.com/api/v10/invites/{invite_code}",
        params={"with_counts": "true"}, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "members": data.get("approximate_member_count"),
        "online_members": data.get("approximate_presence_count"),
    }


def fetch_telegram_public_metrics(channel_username: str) -> dict:
    """Best-effort scrape of a public Telegram channel's subscriber count
    from its public preview page (t.me/s/<channel>) - no bot token needed.
    This is HTML scraping, not an official API, so it's inherently fragile;
    raises instead of guessing if the count can't be found.
    """
    channel_username = channel_username.strip().lstrip("@").rstrip("/").split("/")[-1]
    resp = requests.get(f"https://t.me/s/{channel_username}", timeout=15)
    resp.raise_for_status()
    match = _TELEGRAM_MEMBER_RE.search(resp.text)
    if not match:
        raise ValueError(f"could not find a subscriber count on the public preview page for '{channel_username}'")
    raw = match.group(1).strip().replace(" ", "").replace(",", "")
    multiplier = 1
    if raw.endswith("K"):
        multiplier, raw = 1000, raw[:-1]
    elif raw.endswith("M"):
        multiplier, raw = 1_000_000, raw[:-1]
    return {"members": round(float(raw) * multiplier)}


@tool("read_channel_metrics")
def read_channel_metrics(channel: str, source_url: str = "", metrics_json: str = "{}") -> str:
    """Estimate reach for a channel post/server. channel must be one of:
    reddit, x, discord, telegram, landing_page_direct.

    If source_url is given, tries a keyless public auto-fetch first: a
    Reddit post URL, a Discord invite link, or a Telegram @handle/URL all
    work without any token. Falls back to whatever is in metrics_json if the
    fetch fails or the channel doesn't support it - x has had no free public
    metrics API since 2023 (would need a paid API tier, a "spend" decision),
    and landing_page_direct needs real analytics (e.g. Plausible/GA), not a
    public scrape. metrics_json can also supplement or override fetched
    values, e.g. '{"upvotes": 42}' or '{"impressions": 1500}'.

    Always prefers a real, platform-native number over the fallback formula.
    Returns an error instead of guessing if nothing usable is available -
    never fabricate a reach number.
    """
    try:
        metrics = json.loads(metrics_json) if metrics_json else {}
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    fetch_note = None
    if source_url:
        try:
            if channel == "reddit":
                metrics = {**fetch_reddit_public_metrics(source_url), **metrics}
                fetch_note = "auto-fetched from Reddit's public JSON endpoint"
            elif channel == "discord":
                metrics = {**fetch_discord_public_metrics(source_url), **metrics}
                fetch_note = "auto-fetched from Discord's public invite API"
            elif channel == "telegram":
                metrics = {**fetch_telegram_public_metrics(source_url), **metrics}
                fetch_note = "auto-fetched from Telegram's public channel preview page"
            elif channel == "x":
                fetch_note = "x has had no free public metrics API since 2023 - supply real numbers via metrics_json, or provision a paid X API bearer token"
            elif channel == "landing_page_direct":
                fetch_note = "landing_page_direct needs real analytics (e.g. Plausible/GA), not a public scrape"
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            fetch_note = f"auto-fetch failed ({exc}), falling back to metrics_json"

    try:
        reach, source = scoring.estimate_reach(channel, metrics)
    except ValueError as exc:
        result = {"error": str(exc)}
        if fetch_note:
            result["fetch_note"] = fetch_note
        return json.dumps(result)

    result = {"channel": channel, "estimated_reach": reach, "reach_source": source}
    if fetch_note:
        result["fetch_note"] = fetch_note
    return json.dumps(result)


@tool("compare_channel_performance")
def compare_channel_performance() -> str:
    """Rank channels by average score across all evaluated hypotheses that
    used them, using real historical data from hypotheses.jsonl - grounding
    for deciding where to invest effort or ad spend next instead of
    guessing. Roster channels (see read_channels) with no evaluated
    hypotheses yet are listed separately as untested, never silently scored
    as zero.
    """
    hyps = _read_jsonl("hypotheses.jsonl")
    by_channel = {}
    for h in hyps:
        channel = h.get("channel")
        if not channel or h.get("status") != "evaluated" or h.get("score") is None:
            continue
        by_channel.setdefault(channel, []).append(h["score"])

    ranked = sorted(
        (
            {"channel": ch, "average_score": round(sum(scores) / len(scores), 3), "evaluated_hypotheses": len(scores)}
            for ch, scores in by_channel.items()
        ),
        key=lambda row: row["average_score"],
        reverse=True,
    )
    roster_ids = {c["id"] for c in _read_jsonl("channels.jsonl") if "id" in c}
    untested = sorted(roster_ids - by_channel.keys())
    return json.dumps({"ranked": ranked, "untested_channels": untested})


# --------------------------------------------------------------------------
# Dev tool
# --------------------------------------------------------------------------

@tool("open_pull_request")
def open_pull_request(branch_name: str, file_path: str, file_content: str, pr_title: str, pr_body: str) -> str:
    """Create a new file on a new branch off main and open a PR - never
    edits index.html directly and never merges anything itself; making a
    variant live is always a separate, human-approved step. Requires
    GITHUB_TOKEN (repo-scoped) in the environment; returns a clear
    "not configured" error instead of pretending to succeed if it's missing.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return json.dumps({
            "error": "GITHUB_TOKEN not set - cannot open a PR. Needs to be provisioned by the "
                     "board in Railway's environment variables (repo scope only)."
        })

    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}

    ref_resp = requests.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/git/ref/heads/main", headers=headers, timeout=15)
    if ref_resp.status_code != 200:
        return json.dumps({"error": f"could not read main ref: {ref_resp.status_code} {ref_resp.text}"})
    base_sha = ref_resp.json()["object"]["sha"]

    branch_resp = requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs", headers=headers, timeout=15,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    )
    if branch_resp.status_code not in (200, 201):
        return json.dumps({"error": f"could not create branch: {branch_resp.status_code} {branch_resp.text}"})

    content_b64 = base64.b64encode(file_content.encode("utf-8")).decode("ascii")
    file_resp = requests.put(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{file_path}", headers=headers, timeout=15,
        json={"message": f"Add landing page variant: {file_path}", "content": content_b64, "branch": branch_name},
    )
    if file_resp.status_code not in (200, 201):
        return json.dumps({"error": f"could not create file: {file_resp.status_code} {file_resp.text}"})

    pr_resp = requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls", headers=headers, timeout=15,
        json={"title": pr_title, "body": pr_body, "head": branch_name, "base": "main"},
    )
    if pr_resp.status_code not in (200, 201):
        return json.dumps({"error": f"could not open PR: {pr_resp.status_code} {pr_resp.text}"})

    return json.dumps({"pr_url": pr_resp.json()["html_url"]})


# --------------------------------------------------------------------------
# Web research: search + read (structural-rebuild addendum, section 1).
# Before this, no agent in this system could read actual external content -
# log_research_finding was write-only, read_channel_metrics returns counts
# never thread text, search_research_archive only searches this system's own
# past records. That made a genuinely new topic's research artifact
# (section 5.11) impossible to satisfy honestly: an own_question_post's
# replies were the only real external content an agent could ever read, but
# that's the community_engagement stage's own artifact, not research's -
# real circularity, not just an awkward reading. These two tools resolve it:
# passive discovery (no posting required) now genuinely exists for the
# research stage; own_question_post_replies remains valid as a real, later,
# supplementary confirmation once community_engagement has already happened,
# not the only way to ever get a research artifact.
#
# Hand-rolled with `requests`/`bs4` directly (not the crewai-tools package):
# same cost as crewai-tools' SerperDevTool+ScrapeWebsiteTool (one new
# API key, matching this repo's existing not-yet-configured-tool
# pattern, e.g. GITHUB_TOKEN/open_pull_request) without pulling in
# crewai-tools' unrelated dependencies (pymupdf, pytube,
# youtube-transcript-api, tiktoken) for a two-tool use case - consistent
# with every other tool in this file already being a plain requests call,
# and with this project's own stated preference for direct tool use over
# framework layers.
# --------------------------------------------------------------------------

SERPER_SEARCH_URL = "https://google.serper.dev/search"
READ_WEBPAGE_MAX_CHARS = 6000


@tool("search_web")
def search_web(query: str, num_results: int = 5) -> str:
    """Search the public web for a query - finds relevant pages/threads
    before reading any of them (pair with read_webpage). Real search
    results (title/link/snippet), never invented. Requires API-Sentinel-serper
    (serper.dev) in the environment; returns a clear "not configured" error
    instead of pretending to succeed if it's missing, same pattern as
    open_pull_request's GITHUB_TOKEN check.

    This is the passive-discovery path for evidence_stage='research'
    (section 5.11) - use it and read_webpage to actually find and read real
    content before calling log_research_finding, rather than only having
    own_question_post_replies (which requires community_engagement to have
    already happened) as the sole route to a real research artifact.
    """
    # Env var name is "API-Sentinel-serper" (hyphens, mixed case) - this is
    # the exact name actually provisioned in Railway, not a code convention
    # choice. os.environ.get() does an exact string match regardless of
    # case/hyphens, so it works fine - but don't silently "clean it up" to
    # SERPER_API_KEY or similar, that would break the real Railway variable.
    api_key = os.environ.get("API-Sentinel-serper")
    if not api_key:
        return json.dumps({
            "error": "API-Sentinel-serper not set - cannot search the web. Needs to be provisioned by the "
                     "board in Railway's environment variables (a serper.dev account/key)."
        })
    num_results = max(1, min(int(num_results), 10))
    try:
        resp = requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return json.dumps({"error": f"web search failed: {exc}"})

    results = [
        {"title": r.get("title"), "link": r.get("link"), "snippet": r.get("snippet")}
        for r in (data.get("organic") or [])[:num_results]
    ]
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)


@tool("read_webpage")
def read_webpage(url: str) -> str:
    """Fetch a specific URL and return its actual visible text content
    (scripts/styles/nav stripped), truncated to READ_WEBPAGE_MAX_CHARS
    characters - real page content to paraphrase into log_research_finding,
    never fabricated. No API key needed (plain HTTP fetch + parse). Pair
    with search_web to find URLs worth reading, or read_webpage a URL found
    some other way (e.g. from read_channel_metrics or a known site).

    Returns an error instead of guessing if the fetch fails (blocked,
    timeout, non-HTML content) - never invent what a page "probably" says.
    """
    try:
        resp = requests.get(
            url, headers={"User-Agent": "APISentinel-Research/1.0 (+https://github.com/evolution5s/api-sentinel)"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"error": f"could not fetch '{url}': {exc}"})

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return json.dumps({"error": f"'{url}' is not readable text/html content (content-type: {content_type})"})

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    truncated = len(text) > READ_WEBPAGE_MAX_CHARS
    return json.dumps({
        "url": url,
        "text": text[:READ_WEBPAGE_MAX_CHARS],
        "truncated": truncated,
    }, ensure_ascii=False)


# --------------------------------------------------------------------------
# Research-evidence tier (Validated Learning addendum, section 4) - a
# cheaper, weaker-than-a-live-experiment validation step: existing
# competitor products, forum discussions, a genuine question post's
# replies (the latter only possible once community_engagement has actually
# happened - a real, later confirmation, not a way to bootstrap a research
# artifact from nothing; see search_web/read_webpage above for that).
# Logged for reasoning context; never enough on its own to push a
# hypothesis to "build" - only evaluate_hypothesis's real score off
# signups.jsonl does that.
# --------------------------------------------------------------------------

RESEARCH_FINDING_TYPES = {"competitor_product", "forum_discussion", "own_question_post_replies", "other"}


@tool("log_research_finding")
def log_research_finding(hypothesis_id: str, finding_type: str, source: str, summary: str) -> str:
    """Log a piece of research evidence tied to a hypothesis - cheaper and
    faster than a live experiment, and a sensible default first step
    before proposing one. Examples: an existing competitor product,
    a relevant forum discussion you found, or the replies you got on a
    genuine question post (draft_content's "own_question_post" post_type is
    itself a validation method that feeds this tier).

    finding_type must be one of competitor_product/forum_discussion/
    own_question_post_replies/other. This is weaker evidence than a live
    experiment - it can support reasoning
    toward "test_further" or "pivot", but evaluate_hypothesis's real score
    (from actual signups.jsonl conversions) is still the only thing that
    can produce a "build" outcome.

    summary must be a real, retrievable artifact (section 3), at least
    RESEARCH_FINDING_MIN_LENGTH characters - which threads/posts, what they
    said (paraphrased, never fabricated), how many, how recent - or an
    honest, equally specific negative result ("searched X/Y/Z terms across
    these channels/communities, found no substantive evidence"). A one-liner
    doesn't qualify; write_hypothesis's evidence_stage gate checks entries
    logged here for this substance bar before letting a hypothesis progress
    past 'research'. Also rejected if summary echoes known instruction/
    incident template phrasing rather than this hypothesis's own findings
    (section 5).
    """
    if finding_type not in RESEARCH_FINDING_TYPES:
        return json.dumps({
            "error": f"invalid finding_type '{finding_type}', must be one of {sorted(RESEARCH_FINDING_TYPES)}"
        })
    if len(summary.strip()) < RESEARCH_FINDING_MIN_LENGTH:
        return json.dumps({
            "error": f"summary must be at least {RESEARCH_FINDING_MIN_LENGTH} characters - a real, "
                     "retrievable artifact (which threads/posts, what they said, how many, how recent - "
                     "or an equally specific negative result), not a one-liner claim"
        })
    _echoed = _instruction_echo_match(summary)
    if _echoed:
        return json.dumps({
            "error": f"summary echoes known instruction/incident template language ('{_echoed}') rather "
                     "than this hypothesis's own findings - describe what was actually found, don't reuse "
                     "example wording"
        })
    record = {
        "id": f"research_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hypothesis_id": hypothesis_id,
        "finding_type": finding_type,
        "source": source,
        "summary": summary,
    }
    _append_jsonl("research_findings.jsonl", record)
    return json.dumps({"ok": True, "id": record["id"]})


@tool("read_research_findings")
def read_research_findings(hypothesis_id: str = "") -> str:
    """Return logged research-evidence records from research_findings.jsonl,
    optionally filtered to one hypothesis_id, or "" for all.
    """
    findings = _read_jsonl("research_findings.jsonl")
    if hypothesis_id:
        findings = [f for f in findings if f.get("hypothesis_id") == hypothesis_id]
    return json.dumps(findings, ensure_ascii=False)


# --------------------------------------------------------------------------
# Content drafting (Channel Selection addendum) - organic community content
# only (cold email is excluded, not just for style but for EU
# ePrivacy/GDPR/German UWG legal reasons). Drafting is separate from
# posting: a human always posts by hand and confirms it via a Telegram
# "posted: <draft_id> <url>" reply, same pattern as the existing
# approve/reject/stop/start remote control.
# --------------------------------------------------------------------------

CONTENT_PLATFORMS = {"reddit", "discord", "quant_stackexchange", "forum_other"}
CONTENT_POST_TYPES = {"thread_reply", "own_question_post", "own_hypothesis_post"}
CONTENT_LENGTH_CAPS = {"thread_reply": 600, "own_question_post": 500, "own_hypothesis_post": 1500}
_AI_TELL_PATTERNS = [
    (re.compile(r"^#{1,6}\s", re.MULTILINE), "a markdown header"),
    (re.compile(r"^\s*[-*]\s", re.MULTILINE), "a bullet-list line"),
    (re.compile(r"\bin conclusion\b", re.IGNORECASE), '"in conclusion"'),
    (re.compile(r"\bin summary\b", re.IGNORECASE), '"in summary"'),
    (re.compile(r"\bas an ai\b", re.IGNORECASE), '"as an AI"'),
    (re.compile(r"\bi hope this helps\b", re.IGNORECASE), '"I hope this helps"'),
]


def _find_style_violations(text: str) -> list:
    return [f"contains {label} - reads as AI-generated, not a quick human post" for pattern, label in _AI_TELL_PATTERNS if pattern.search(text)]


@tool("draft_content")
def draft_content(
    hypothesis_id: str, platform: str, post_type: str, target_community: str, text: str,
    is_promotional: bool, include_product_link: bool, rules_checked: bool, rules_notes: str,
) -> str:
    """Draft one piece of organic community content (your own words, not
    boilerplate) for human review before anything is posted. This tool
    only writes a draft to content_drafts.jsonl with status="drafted" - it
    never posts anything itself and does not request approval on its own;
    follow up with request_approval(category="publish"), then wait for a
    human "posted: <draft_id> <url>" Telegram reply before treating it as
    live.

    platform: one of reddit/discord/quant_stackexchange/forum_other. Cold
    email is not a supported platform here - excluded for legal reasons (EU
    ePrivacy/GDPR, German UWG), not a style choice, so don't try to route
    around it via forum_other.

    post_type: one of thread_reply/own_question_post/own_hypothesis_post
    - "thread_reply": a genuine reply inside someone else's existing thread.
    - "own_question_post": a real, curious question - itself a validation
      method (log_research_finding's own_question_post_replies tier), not
      promotion. Usually should NOT include_product_link.
    - "own_hypothesis_post": you starting a thread about the problem/idea
      itself.

    Enforced, mechanical checks (tone/quality judgment beyond this is
    yours - these only catch what a heuristic actually can catch):
    - rules_checked must be true, with non-empty rules_notes describing
      what you found - check THIS community's current self-promotion
      rules before EVERY post, not once per platform; rules vary a lot
      between communities and change over time.
    - text must not read like an LLM wrote it: no markdown headers, no
      bullet lists, no "in conclusion"/"in summary"/"as an AI"/"I hope
      this helps". Short, plain, imperfect prose only - like a moderately
      engaged human typed it, not a report.
    - length cap by post_type: thread_reply<=600, own_question_post<=500,
      own_hypothesis_post<=1500 chars - a real forum reply is a paragraph,
      not an essay.
    - include_product_link requires hypothesis_id to point at a hypothesis
      with landing_page_live=true - never link to a page that doesn't
      exist yet. That flag is only set by a human "live: <hypothesis_id>"
      Telegram reply once a PR is actually merged (merging is always a
      human step), so the system never fabricates "live" on its own.
    """
    if platform not in CONTENT_PLATFORMS:
        return json.dumps({"error": f"invalid platform '{platform}', must be one of {sorted(CONTENT_PLATFORMS)}"})
    if post_type not in CONTENT_POST_TYPES:
        return json.dumps({"error": f"invalid post_type '{post_type}', must be one of {sorted(CONTENT_POST_TYPES)}"})
    if not rules_checked:
        return json.dumps({
            "error": "rules_checked must be true - check this community's own posting/self-promo rules "
                     "before drafting, every single time, not just once per platform"
        })
    if not rules_notes.strip():
        return json.dumps({"error": "rules_notes must describe what you found when checking this community's rules"})
    if not target_community.strip():
        return json.dumps({"error": "target_community must not be empty"})

    violations = _find_style_violations(text)
    length_cap = CONTENT_LENGTH_CAPS[post_type]
    if len(text) > length_cap:
        violations.append(f"{len(text)} chars exceeds the {length_cap}-char cap for post_type={post_type}")
    if violations:
        return json.dumps({"error": "draft rejected by style checks", "violations": violations})

    hyps = _read_jsonl("hypotheses.jsonl")
    hyp = next((h for h in hyps if h.get("id") == hypothesis_id), None)
    if hyp is None:
        return json.dumps({"error": f"no hypothesis with id '{hypothesis_id}'"})
    if include_product_link and not hyp.get("landing_page_live"):
        return json.dumps({
            "error": "include_product_link=true but this hypothesis's landing_page_live is not true yet "
                     "- draft without the link, or wait for the human 'live: <hypothesis_id>' confirmation"
        })

    draft_id = f"draft_{uuid.uuid4().hex[:8]}"
    record = {
        "id": draft_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hypothesis_id": hypothesis_id,
        "platform": platform,
        "post_type": post_type,
        "target_community": target_community,
        "text": text,
        "is_promotional": is_promotional,
        "include_product_link": include_product_link,
        "rules_checked": rules_checked,
        "rules_notes": rules_notes,
        "status": "drafted",
        "approved_request_id": None,
        "posted_at": None,
        "post_url": None,
        "removed": False,
        "removed_reason": None,
        "removed_at": None,
    }
    _append_jsonl("content_drafts.jsonl", record)
    return json.dumps({"ok": True, "id": draft_id, "status": "drafted"})


@tool("read_content_drafts")
def read_content_drafts(hypothesis_id: str = "", status: str = "") -> str:
    """Return drafted/posted/removed content records from
    content_drafts.jsonl, optionally filtered by hypothesis_id and/or
    status (drafted/posted/removed).
    """
    drafts = _read_jsonl("content_drafts.jsonl")
    if hypothesis_id:
        drafts = [d for d in drafts if d.get("hypothesis_id") == hypothesis_id]
    if status:
        drafts = [d for d in drafts if d.get("status") == status]
    return json.dumps(drafts, ensure_ascii=False)


@tool("check_community_risk")
def check_community_risk(platform: str, target_community: str) -> str:
    """Check this specific community's recent post-removal history before
    posting again - a removed post is a real signal, not noise. Counts
    status="removed" drafts for this platform+community in the last 30
    days; risk="high" at 2+ removals in that window (treat as a cooldown
    signal / rethink the approach there), else "low". Read
    recent_removals' reasons, not just the count, before deciding what to
    change.
    """
    if platform not in CONTENT_PLATFORMS:
        return json.dumps({"error": f"invalid platform '{platform}', must be one of {sorted(CONTENT_PLATFORMS)}"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    recent_removals = []
    for d in _read_jsonl("content_drafts.jsonl"):
        if d.get("platform") != platform or d.get("target_community") != target_community or not d.get("removed"):
            continue
        removed_at = d.get("removed_at")
        try:
            if removed_at and datetime.fromisoformat(removed_at) >= cutoff:
                recent_removals.append({
                    "id": d.get("id"), "removed_at": removed_at, "removed_reason": d.get("removed_reason"),
                })
        except ValueError:
            continue
    risk = "high" if len(recent_removals) >= 2 else "low"
    return json.dumps({
        "platform": platform, "target_community": target_community,
        "removal_count_last_30d": len(recent_removals), "risk": risk, "recent_removals": recent_removals,
    })


@tool("get_account_stats")
def get_account_stats(platform: str) -> str:
    """Return this platform's genuine-vs-promotional content ratio across
    all drafted content (one account per product per platform, so this is
    effectively that account's own stats). Target is roughly 90% genuinely
    useful/curious content to 10% promotional, tracked on a rolling basis
    across all drafts - not a per-post rule.
    """
    if platform not in CONTENT_PLATFORMS:
        return json.dumps({"error": f"invalid platform '{platform}', must be one of {sorted(CONTENT_PLATFORMS)}"})
    drafts = [d for d in _read_jsonl("content_drafts.jsonl") if d.get("platform") == platform]
    total = len(drafts)
    promotional = sum(1 for d in drafts if d.get("is_promotional"))
    return json.dumps({
        "platform": platform,
        "total_posts_drafted": total,
        "promotional_count": promotional,
        "genuine_count": total - promotional,
        "promotional_ratio": round(promotional / total, 3) if total else None,
        "target_promotional_ratio": 0.10,
    })


# --------------------------------------------------------------------------
# Cycle notification (orchestration-level, not an agent tool - called
# directly from crew.py after kickoff() so delivery never depends on an
# agent remembering to call it)
# --------------------------------------------------------------------------

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(text: str, parse_mode: str = None) -> None:
    """Send a message via a Telegram bot to a fixed chat, split across
    multiple messages if it exceeds Telegram's 4096-char limit. Needs
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment; prints a
    clear warning and returns quietly if either is missing or the send
    fails - a missing/failed notification must never crash the crew run
    that already completed successfully.

    parse_mode (e.g. "Markdown") enables formatting (used for the fixed-
    width token/cost table, see crew.py's _format_usage_table). If a
    formatted send is ever rejected by Telegram (e.g. a parse error),
    automatically retries that same chunk once as plain text instead of
    losing the message - formatting must degrade gracefully, never cost
    the report itself.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - skipping cycle summary notification.")
        return

    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH):
        chunk = text[i:i + TELEGRAM_MAX_MESSAGE_LENGTH]
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            if parse_mode:
                print(f"[telegram] formatted send failed ({exc}), retrying this chunk as plain text")
                try:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    continue
                except requests.RequestException as retry_exc:
                    print(f"[telegram] plain-text retry also failed: {retry_exc}")
                    return
            print(f"[telegram] failed to send cycle summary: {exc}")
            return


# --------------------------------------------------------------------------
# Hypothesis overview (orchestration-level, not an agent tool - built
# directly by crew.py's send_cycle_summary, structural-rebuild addendum
# section 8). The missing "saubere Auswertung": a compact, scannable table
# of every active hypothesis near the top of the cycle report, readable
# without parsing each agent's full narrative output.
# --------------------------------------------------------------------------

def build_hypothesis_overview() -> list:
    """One entry per active hypothesis: id, evidence_stage, a one-line
    status, the most recent logged research finding (or an honest "none
    yet"), and the next concrete action. Never fabricates - every field
    traces to an actual record, same ground-truth discipline as everything
    else this system reports.

    next_action is the MOST RECENTLY filed open task order, not the
    oldest (2026-08-11 fix: task orders have no evidence_stage snapshot on
    them, so there's no reliable way to tell whether an old open order is
    still valid for a hypothesis's current stage - but an order's own
    recency is at least a real, ground-truth signal, and is a strictly
    better default than surfacing whatever happens to have been open
    longest, which can be an order from a stage the hypothesis has since
    moved past, or from before evidence-stage gating existed at all. If
    more than one order is open, the count is appended rather than
    silently hidden - orders piling up unclosed is itself a real signal,
    not something this function should paper over.
    """
    findings = _read_jsonl("research_findings.jsonl")
    orders = _read_jsonl("task_orders.jsonl")
    overview = []
    for h in _read_jsonl("hypotheses.jsonl"):
        if h.get("status") != "active":
            continue
        hyp_id = h.get("id")
        own_findings = sorted(
            (f for f in findings if f.get("hypothesis_id") == hyp_id),
            key=lambda f: f.get("created_at") or "",
        )
        latest_finding = own_findings[-1]["summary"][:140] if own_findings else "keine Erkenntnis geloggt"
        open_orders = sorted(
            (o for o in orders if o.get("hypothesis_id") == hyp_id and o.get("status") == "open"),
            key=lambda o: o.get("created_at") or "",
        )
        if open_orders:
            next_action = open_orders[-1]["task_description"][:140]
            if len(open_orders) > 1:
                next_action += f" (+{len(open_orders) - 1} weitere offene Order(n))"
        else:
            next_action = "keine offene Task-Order"
        overview.append({
            "id": hyp_id,
            "evidence_stage": h.get("evidence_stage") or "(nicht gesetzt)",
            "status_line": f"{h.get('category', '?')} / {h.get('channel', '?')}, seit {(h.get('created_at') or '?')[:10]}",
            "latest_finding": latest_finding,
            "next_action": next_action,
        })
    return overview


# --------------------------------------------------------------------------
# State-persistence check (orchestration-level, not an agent tool - called
# once at the very start of every cycle, before anything else, so a warning
# can reach Telegram even if the rest of the cycle then fails). Root cause
# this exists for: STATE_DIR defaulting to /data looks identical whether or
# not a real Railway Volume is actually mounted there - the only way this
# repo found out no volume existed for a long stretch of its history was by
# manually diffing Railway logs across deploys. This makes that check
# automatic instead of something a human has to remember to do by hand.
# --------------------------------------------------------------------------

def check_state_persistence() -> dict:
    """Deterministic check for whether STATE_DIR is genuinely backed by a
    mounted Railway Volume, using RAILWAY_VOLUME_MOUNT_PATH - a real,
    Railway-documented runtime env var populated "if any" volume is
    attached (https://docs.railway.com/variables/reference), not a guess.
    Never raises.

    Outside Railway (no RAILWAY_ENVIRONMENT_ID set - local runs, tests)
    this check isn't applicable at all: returns applicable=False,
    persistent=True, warning=None, since there's no volume concept to
    check outside Railway and a false warning there would just be noise.

    Inside Railway: persistent=True only if RAILWAY_VOLUME_MOUNT_PATH is
    set AND resolves to the same path as STATE_DIR. Otherwise
    persistent=False with a concrete warning string - state will not
    survive the next redeploy (a new deployment is a fresh container image
    with no continuity from the previous one's writable layer; only an
    actually-mounted volume survives that, per this repo's own confirmed
    incident - see README chapter 15).
    """
    if not os.getenv("RAILWAY_ENVIRONMENT_ID"):
        return {"applicable": False, "persistent": True, "warning": None}

    volume_mount_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    volume_name = os.getenv("RAILWAY_VOLUME_NAME")

    if volume_mount_path and Path(volume_mount_path) == STATE_DIR:
        return {
            "applicable": True, "persistent": True, "warning": None,
            "volume_name": volume_name, "volume_mount_path": volume_mount_path,
        }

    return {
        "applicable": True, "persistent": False,
        "warning": (
            f"STATE_DIR ({STATE_DIR}) does not match a mounted Railway Volume "
            f"(RAILWAY_VOLUME_MOUNT_PATH={volume_mount_path!r}) - all state "
            "will be lost on the next redeploy. Attach a Railway Volume "
            f"mounted at {STATE_DIR}, or fix STATE_DIR to match the one "
            "that's actually mounted."
        ),
        "volume_name": volume_name, "volume_mount_path": volume_mount_path,
    }


# --------------------------------------------------------------------------
# Telegram remote control (orchestration-level, not an agent tool - checked
# directly from crew.py before each cron run, same reasoning as above: an
# agent must never decide for itself whether to listen for operator
# commands). Two things an operator can do by replying in Telegram, reusing
# the existing human approval queue rather than adding a second one:
#   - "stop"/"start": pause/resume the whole system across cron cycles.
#   - reply "approve"/"reject" to a pending-approval notification (or type
#     "<id> approve" directly): same effect as running approve.py.
# --------------------------------------------------------------------------

_TELEGRAM_OFFSET_FILE = "telegram_update_offset.txt"
_SYSTEM_PAUSE_FILE = "system_paused.json"
_APPROVAL_ID_RE = re.compile(r"appr_[0-9a-f]{8}")
_DRAFT_ID_RE = re.compile(r"draft_[0-9a-f]{8}")
_STOP_WORDS = {"stop", "pause"}
_START_WORDS = {"start", "resume", "weiter"}
_APPROVE_WORDS = {"approve", "ja", "yes"}
_REJECT_WORDS = {"reject", "nein", "no"}
_DURATION_POLICY_STAGE_ORDER = ("research", "community_engagement", "landing_page", "build")
_FIX_THRESHOLD_ORDER = (
    "zero_state_streak_cycles", "malformed_tool_calls_cycles", "channel_bury_streak",
    "repeated_pivot_streak", "stale_approval_hours",
)


def is_system_paused() -> tuple[bool, str]:
    """Whether the system is currently paused via a Telegram 'stop' command,
    and the note explaining why. This is durable, cross-cycle state (not
    per-run) - it stays paused until an explicit 'start'/'resume' clears it,
    same file survives across every 6h cron invocation.
    """
    path = STATE_DIR / _SYSTEM_PAUSE_FILE
    if not path.exists():
        return False, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, ""
    return bool(data.get("paused")), data.get("note", "")


def set_system_paused(paused: bool, note: str = "") -> None:
    _ensure_state_dir()
    (STATE_DIR / _SYSTEM_PAUSE_FILE).write_text(
        json.dumps(
            {"paused": paused, "since": datetime.now(timezone.utc).isoformat(), "note": note},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_telegram_offset() -> int:
    path = STATE_DIR / _TELEGRAM_OFFSET_FILE
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def _write_telegram_offset(offset: int) -> None:
    _ensure_state_dir()
    (STATE_DIR / _TELEGRAM_OFFSET_FILE).write_text(str(offset), encoding="utf-8")


def _fetch_telegram_updates() -> list:
    """Fetch new Telegram messages since the last processed update, scoped
    to TELEGRAM_CHAT_ID only (never trust any other chat). Advances the
    persisted offset immediately after a successful fetch, even for
    messages that turn out not to be recognized commands, so a stray
    message can never wedge processing in a retry loop. Returns [] and
    never raises on any failure - Telegram being unreachable must never
    block or crash a cron cycle.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return []

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _read_telegram_offset()},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json().get("result", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"[telegram] failed to fetch updates: {exc}")
        return []

    if not result:
        return []
    _write_telegram_offset(result[-1]["update_id"] + 1)

    messages = []
    for update in result:
        message = update.get("message") or {}
        if str(message.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        text = (message.get("text") or "").strip()
        if not text:
            continue
        reply_to = message.get("reply_to_message") or {}
        messages.append({"text": text, "reply_to_text": reply_to.get("text") or ""})
    return messages


def _classify_command(text: str, reply_to_text: str):
    """Classify one already-fetched Telegram message as a command. Returns
    (action, payload) or None if the message isn't a recognized command -
    the operator may just be chatting, that's not an error, it's silently
    ignored. Payload shape depends on action:
    - "pause"/"resume": payload is None.
    - "approve"/"reject": payload is the appr_... approval id.
    - "live": payload is the hypothesis_id to mark landing_page_live=true,
      confirming a PR has actually been merged (a human-only step this
      system otherwise has no way to observe).
    - "posted": payload is (draft_id, url) - a human confirming they
      actually posted a draft from content_drafts.jsonl.
    - "removed": payload is (draft_id, reason) - a human confirming a
      previously-posted draft got taken down, feeding check_community_risk.
    - "payment_link": payload is (approval_id, url) - a human confirming
      they've provisioned the actual payment processor/link for a
      category='spend' payment-intent-test request and handing the URL back.
    - "duration_policy_confirm": payload is None - confirms the currently
      proposed max_duration_days_by_stage policy as-is (DEFAULT_PROPOSED_
      DURATION_CAPS or whatever was last proposed).
    - "duration_policy_set": payload is a dict of stage -> days (or None for
      no cap) - sets custom values and confirms in the same step.
    - "stagnation_ack": payload is the subsidiary_id - a human acknowledging
      a persistent stagnation escalation (section 3), clearing it from the
      "Fuer den Aufsichtsrat" section until it would genuinely re-trigger.

    Two ways to approve/reject: reply directly to the notification message
    that announced the pending approval (matched via the appr_... id in
    reply_to_text), or type "<id> approve"/"<id> reject" directly if that
    original message has scrolled out of view. "live:"/"posted:"/
    "removed:"/"payment_link:" are always typed directly (there's no single
    notification message to reply to for those), as "live: <hypothesis_id>",
    "posted: <draft_id> <url>", "removed: <draft_id> <reason>",
    "payment_link: <appr_id> <url>".
    """
    normalized = text.strip().lower()

    if normalized in _STOP_WORDS:
        return "pause", None
    if normalized in _START_WORDS:
        return "resume", None

    decision = "approve" if normalized in _APPROVE_WORDS else "reject" if normalized in _REJECT_WORDS else None
    if decision:
        match = _APPROVAL_ID_RE.search(reply_to_text)
        return (decision, match.group(0)) if match else None

    id_match = _APPROVAL_ID_RE.search(text)
    if id_match:
        remainder = normalized.replace(id_match.group(0).lower(), "").strip()
        if remainder in _APPROVE_WORDS:
            return "approve", id_match.group(0)
        if remainder in _REJECT_WORDS:
            return "reject", id_match.group(0)

    if normalized.startswith("live:"):
        hypothesis_id = text.split(":", 1)[1].strip()
        return ("live", hypothesis_id) if hypothesis_id else None

    if normalized.startswith("posted:"):
        rest = text.split(":", 1)[1].strip()
        draft_match = _DRAFT_ID_RE.search(rest)
        if not draft_match:
            return None
        url = rest[draft_match.end():].strip()
        return ("posted", (draft_match.group(0), url)) if url else None

    if normalized.startswith("removed:"):
        rest = text.split(":", 1)[1].strip()
        draft_match = _DRAFT_ID_RE.search(rest)
        if not draft_match:
            return None
        reason = rest[draft_match.end():].strip() or "removed (no reason given)"
        return "removed", (draft_match.group(0), reason)

    if normalized.startswith("payment_link:"):
        rest = text.split(":", 1)[1].strip()
        approval_match = _APPROVAL_ID_RE.search(rest)
        if not approval_match:
            return None
        url = rest[approval_match.end():].strip()
        return ("payment_link", (approval_match.group(0), url)) if url else None

    if normalized.startswith("duration_policy:"):
        rest = text.split(":", 1)[1].strip()
        if rest.lower() == "confirm":
            return ("duration_policy_confirm", None)
        parts = rest.split()
        if len(parts) == len(_DURATION_POLICY_STAGE_ORDER):
            values = {}
            for stage, raw in zip(_DURATION_POLICY_STAGE_ORDER, parts):
                if raw.lower() == "none":
                    values[stage] = None
                    continue
                try:
                    values[stage] = int(raw)
                except ValueError:
                    return None
            return ("duration_policy_set", values)
        return None

    if normalized.startswith("stagnation_ack:"):
        subsidiary_id = text.split(":", 1)[1].strip()
        return ("stagnation_ack", subsidiary_id) if subsidiary_id else None

    if normalized.startswith("fix_resolved:"):
        entry_id = text.split(":", 1)[1].strip()
        return ("fix_resolved", entry_id) if entry_id else None

    if normalized.startswith("fix_thresholds:"):
        rest = text.split(":", 1)[1].strip()
        if rest.lower() == "confirm":
            return ("fix_thresholds_confirm", None)
        parts = rest.split()
        if len(parts) == len(_FIX_THRESHOLD_ORDER):
            values = {}
            for key, raw in zip(_FIX_THRESHOLD_ORDER, parts):
                try:
                    values[key] = int(raw)
                except ValueError:
                    return None
            return ("fix_thresholds_set", values)
        return None

    return None


def _apply_telegram_commands(messages: list) -> list:
    """Apply already-parsed Telegram commands: mutate the pause flag and/or
    the approval queue, and send a short confirmation back per command.
    Split out from the network fetch so the command logic itself is
    testable with a canned message list, without hitting Telegram. Returns
    a human-readable log of what happened, for the cycle summary.
    """
    import approve  # local import: avoids a circular import (approve.py imports from tools)

    log = []
    records = None

    for msg in messages:
        command = _classify_command(msg.get("text", ""), msg.get("reply_to_text", ""))
        if command is None:
            continue
        action, target_id = command

        if action == "pause":
            set_system_paused(True, "per Telegram-Kommando 'stop'")
            log.append("System pausiert (Telegram)")
            send_telegram_message("System pausiert. Sende 'start', um fortzufahren.")
            continue
        if action == "resume":
            set_system_paused(False)
            log.append("System fortgesetzt (Telegram)")
            send_telegram_message("System fortgesetzt.")
            continue

        if action == "live":
            hypothesis_id = target_id
            hyps = _read_jsonl("hypotheses.jsonl")
            idx = next((i for i, h in enumerate(hyps) if h.get("id") == hypothesis_id), None)
            if idx is None:
                send_telegram_message(f"Keine Hypothese mit id '{hypothesis_id}' gefunden.")
                continue
            hyps[idx]["landing_page_live"] = True
            _write_jsonl("hypotheses.jsonl", hyps)
            log.append(f"{hypothesis_id} landing_page_live=true (Telegram)")
            send_telegram_message(f"{hypothesis_id}: landing_page_live gesetzt.")
            continue

        if action == "posted":
            draft_id, url = target_id
            drafts = _read_jsonl("content_drafts.jsonl")
            idx = next((i for i, d in enumerate(drafts) if d.get("id") == draft_id), None)
            if idx is None:
                send_telegram_message(f"Kein Draft mit id '{draft_id}' gefunden.")
                continue
            drafts[idx]["status"] = "posted"
            drafts[idx]["posted_at"] = datetime.now(timezone.utc).isoformat()
            drafts[idx]["post_url"] = url
            _write_jsonl("content_drafts.jsonl", drafts)
            log.append(f"{draft_id} als gepostet markiert (Telegram)")
            send_telegram_message(f"{draft_id}: als gepostet markiert ({url}).")
            continue

        if action == "removed":
            draft_id, reason = target_id
            drafts = _read_jsonl("content_drafts.jsonl")
            idx = next((i for i, d in enumerate(drafts) if d.get("id") == draft_id), None)
            if idx is None:
                send_telegram_message(f"Kein Draft mit id '{draft_id}' gefunden.")
                continue
            drafts[idx]["status"] = "removed"
            drafts[idx]["removed"] = True
            drafts[idx]["removed_reason"] = reason
            drafts[idx]["removed_at"] = datetime.now(timezone.utc).isoformat()
            _write_jsonl("content_drafts.jsonl", drafts)
            log.append(f"{draft_id} als entfernt markiert (Telegram)")
            send_telegram_message(f"{draft_id}: als entfernt markiert ({reason}).")
            continue

        if action == "payment_link":
            approval_id, url = target_id
            approvals = _read_global_jsonl("approval_queue.jsonl")
            idx = next((i for i, a in enumerate(approvals) if a.get("id") == approval_id), None)
            if idx is None:
                send_telegram_message(f"Keine Freigabe-Anfrage mit id '{approval_id}' gefunden.")
                continue
            if approvals[idx].get("status") != "approved":
                send_telegram_message(
                    f"{approval_id} hat status='{approvals[idx].get('status')}', nicht 'approved' - "
                    "erst freigeben, bevor ein Payment-Link hinterlegt wird."
                )
                continue
            approvals[idx]["payment_link_url"] = url
            approvals[idx]["payment_link_set_at"] = datetime.now(timezone.utc).isoformat()
            _write_global_jsonl("approval_queue.jsonl", approvals)
            log.append(f"{approval_id} payment_link_url gesetzt (Telegram)")
            send_telegram_message(f"{approval_id}: Payment-Link hinterlegt ({url}).")
            continue

        if action == "duration_policy_confirm":
            subs = read_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl")
            idx = next((i for i, s in enumerate(subs) if s.get("id") == _active_subsidiary_id), None)
            if idx is None:
                send_telegram_message("Noch keine Subsidiary-Policy vorhanden - nichts zu bestaetigen.")
                continue
            policies = subs[idx].get("policies") or dict(_SUBSIDIARY_POLICY_DEFAULTS)
            current_policy = policies.get("max_duration_days_by_stage") or DEFAULT_PROPOSED_DURATION_CAPS
            policies["max_duration_days_by_stage"] = {**current_policy, "status": "confirmed"}
            subs[idx]["policies"] = policies
            write_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl", subs)
            log.append("Duration-Policy bestaetigt (Telegram)")
            send_telegram_message(f"Duration-Policy bestaetigt: {policies['max_duration_days_by_stage']['values']}")
            continue

        if action == "duration_policy_set":
            values = target_id
            subs = read_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl")
            idx = next((i for i, s in enumerate(subs) if s.get("id") == _active_subsidiary_id), None)
            if idx is None:
                send_telegram_message("Noch keine Subsidiary-Policy vorhanden - kann noch nicht gesetzt werden.")
                continue
            policies = subs[idx].get("policies") or dict(_SUBSIDIARY_POLICY_DEFAULTS)
            policies["max_duration_days_by_stage"] = {
                "status": "confirmed", "values": values, "note": "Set and confirmed via Telegram.",
            }
            subs[idx]["policies"] = policies
            write_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl", subs)
            log.append("Duration-Policy gesetzt und bestaetigt (Telegram)")
            send_telegram_message(f"Duration-Policy gesetzt und bestaetigt: {values}")
            continue

        if action == "stagnation_ack":
            ack_subsidiary_id = target_id
            subs = read_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl")
            idx = next((i for i, s in enumerate(subs) if s.get("id") == ack_subsidiary_id), None)
            if idx is None:
                send_telegram_message(f"Keine Subsidiary mit id '{ack_subsidiary_id}' gefunden.")
                continue
            if not subs[idx].get("stagnation_escalated"):
                send_telegram_message(f"{ack_subsidiary_id}: keine offene Stagnation-Eskalation.")
                continue
            subs[idx]["stagnation_escalated"] = False
            subs[idx]["consecutive_stall_cycles"] = 0
            subs[idx]["stagnation_acknowledged_at"] = datetime.now(timezone.utc).isoformat()
            write_jsonl(STATE_DIR / "_holding", "subsidiaries.jsonl", subs)
            log.append(f"{ack_subsidiary_id} Stagnation-Eskalation bestaetigt (Telegram)")
            send_telegram_message(f"{ack_subsidiary_id}: Stagnation-Eskalation quittiert.")
            continue

        if action == "fix_resolved":
            import holding  # local import: avoids a circular import (holding.py imports from tools)
            ok, message = holding.resolve_fix_entry(target_id)
            if ok:
                log.append(f"{target_id} FIX.md-Eintrag geloest (Telegram)")
            send_telegram_message(message)
            continue

        if action == "fix_thresholds_confirm":
            import holding  # local import: avoids a circular import (holding.py imports from tools)
            confirmed = {**holding.read_fix_thresholds(), "status": "confirmed"}
            holding.write_fix_thresholds(confirmed)
            log.append("FIX-Thresholds bestaetigt (Telegram)")
            send_telegram_message(f"FIX-Thresholds bestaetigt: {confirmed['values']}")
            continue

        if action == "fix_thresholds_set":
            import holding  # local import: avoids a circular import (holding.py imports from tools)
            values = target_id
            confirmed = {"status": "confirmed", "values": values, "note": "Gesetzt und bestaetigt via Telegram."}
            holding.write_fix_thresholds(confirmed)
            log.append("FIX-Thresholds gesetzt und bestaetigt (Telegram)")
            send_telegram_message(f"FIX-Thresholds gesetzt und bestaetigt: {values}")
            continue

        if records is None:
            records = approve._load()
        record = next((r for r in records if r.get("id") == target_id), None)
        if record is None or record.get("status") != "pending":
            continue
        status = "approved" if action == "approve" else "rejected"
        records = approve.decide(records, target_id, status, "via Telegram")
        log.append(f"{target_id} {status} (Telegram)")
        send_telegram_message(f"{target_id}: {status}.")

    if records is not None:
        approve._save(records)

    return log


def process_telegram_commands() -> list:
    """Check Telegram for operator commands sent since the last cycle and
    act on them - see _classify_command for the supported syntax. Always
    runs, even while the system is paused, so a 'start' can still be seen.
    Never raises; a Telegram/network failure must never block or crash a
    cron cycle. Returns a human-readable log of what it did, for the cycle
    summary and Railway logs.
    """
    try:
        messages = _fetch_telegram_updates()
        return _apply_telegram_commands(messages) if messages else []
    except Exception as exc:
        print(f"[telegram] command processing failed: {exc}")
        return []


def _format_publish_proposal(proposal: str) -> str:
    """Render a category='publish' proposal's structured template (section
    7) with fixed German field labels, exactly as filed - never reflowed
    into prose. Falls back to the raw string if it isn't valid template
    JSON (defensive only; request_approval itself already rejects a
    malformed publish proposal before it ever reaches the queue).
    """
    try:
        t = json.loads(proposal)
        if not isinstance(t, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError):
        return proposal
    experiment = "ja" if t.get("is_experiment") else "nein"
    return (
        f"Plattform: {t.get('platform')}\n"
        f"Ziel-URL: {t.get('target_url')}\n"
        f"Titel: {t.get('title')}\n"
        f"Text:\n{t.get('text')}\n"
        f"Footer/Signatur: {t.get('footer')}\n\n"
        f"Gehoert zu: {t.get('hypothesis_id')} (evidence_stage: {t.get('evidence_stage')})\n"
        f"Ist das ein Experiment: {experiment}\n"
        f"Erfolgskriterium: {t.get('success_criterion')}"
    )


def notify_new_pending_approvals() -> None:
    """Send a separate Telegram message for each pending approval that
    hasn't been announced yet, so there's something concrete to reply
    'approve'/'reject' to (see process_telegram_commands). Tracked via a
    telegram_notified flag written onto the record itself so nothing is
    announced twice across cycles. category='publish' proposals are
    rendered via the structured template (_format_publish_proposal,
    section 7) instead of dumped as raw text/JSON - any additional
    reasoning is shown as its own clearly-separated line below the block,
    never mixed into the structured fields.
    """
    approvals = _read_global_jsonl("approval_queue.jsonl")
    changed = False
    for record in approvals:
        if record.get("status") != "pending" or record.get("telegram_notified"):
            continue
        proposal_text = (
            _format_publish_proposal(record.get("proposal") or "")
            if record.get("category") == "publish"
            else f"Antrag: {record.get('proposal')}"
        )
        send_telegram_message(
            f"Neue Freigabe angefragt: {record['id']}\n"
            f"Kategorie: {record.get('category')}\n"
            f"{proposal_text}\n\n"
            f"Begruendung: {record.get('reasoning')}\n\n"
            "Antworte auf diese Nachricht mit 'approve' oder 'reject'."
        )
        record["telegram_notified"] = True
        changed = True
    if changed:
        _write_global_jsonl("approval_queue.jsonl", approvals)
