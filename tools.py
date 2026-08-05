"""State persistence and CrewAI tools shared by all api-sentinel agents.

Everything under STATE_DIR is plain JSONL so it stays human-readable and
diffable. STATE_DIR defaults to /data (the Railway volume mount point) but
can be overridden for local runs and tests.
"""
import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from crewai.tools import tool

import scoring

STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))

GITHUB_REPO = "evolution5s/api-sentinel"
GITHUB_API = "https://api.github.com"
SIGNUP_TITLE_PREFIX = "[Signup]"

APPROVAL_CATEGORIES = {"spend", "legal", "publish", "deploy", "pricing"}
REQUIRED_HYPOTHESIS_FIELDS = {
    "id", "statement", "category", "landing_page_variant_id",
    "failure_rate", "success_rate", "duration_days", "channel",
}
CHANNEL_STATUSES = {"not_tested", "bench", "testing", "retired"}
MAX_CHANNELS_TESTING = 3
MAX_TOTAL_CHANNELS = 20
REQUIRED_CHANNEL_FIELDS = {"id", "name", "category", "is_paid", "impact_score", "confidence_score"}


# --------------------------------------------------------------------------
# STATE_DIR / JSONL primitives
# --------------------------------------------------------------------------

def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(filename: str, record: dict) -> None:
    _ensure_state_dir()
    with (STATE_DIR / filename).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(filename: str) -> list:
    path = STATE_DIR / filename
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _write_jsonl(filename: str, records: list) -> None:
    _ensure_state_dir()
    with (STATE_DIR / filename).open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    a blank slate every 6h. Overwrites - only the latest cycle's note is kept.
    """
    _ensure_state_dir()
    (STATE_DIR / "last_cycle_note.txt").write_text(text, encoding="utf-8")


def read_last_cycle_note() -> str:
    """Return the previous cycle's note, or "" if there isn't one yet (e.g.
    the very first run, or the file predates this feature).
    """
    path = STATE_DIR / "last_cycle_note.txt"
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

    approvals = _read_jsonl("approval_queue.jsonl")
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
    """
    if category not in APPROVAL_CATEGORIES:
        return json.dumps({
            "error": f"invalid category '{category}', must be one of {sorted(APPROVAL_CATEGORIES)}"
        })

    record = {
        "id": f"appr_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "proposal": proposal,
        "reasoning": reasoning,
        "status": "pending",
    }
    _append_jsonl("approval_queue.jsonl", record)
    return json.dumps({"queued": record["id"]})


@tool("read_hypotheses")
def read_hypotheses(status: str = "") -> str:
    """Return hypotheses as JSON. Pass status="active" or status="evaluated"
    to filter; an empty string returns all of them.
    """
    hyps = _read_jsonl("hypotheses.jsonl")
    if status:
        hyps = [h for h in hyps if h.get("status") == status]
    return json.dumps(hyps, ensure_ascii=False)


@tool("write_hypothesis")
def write_hypothesis(hypothesis: str) -> str:
    """Create or update a hypothesis. `hypothesis` must be a JSON string of a
    (possibly partial, if updating an existing one) hypothesis object with at
    least an "id". Creating a new active hypothesis is rejected if its
    landing_page_variant_id already has 2 other active hypotheses (section
    5.6 parallelism rule) - pick a different variant or wait instead.
    """
    try:
        patch = json.loads(hypothesis)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid JSON: {exc}"})

    if "id" not in patch:
        return json.dumps({"error": "hypothesis must include an 'id'"})

    hyps = _read_jsonl("hypotheses.jsonl")
    existing_index = next((i for i, h in enumerate(hyps) if h.get("id") == patch["id"]), None)

    if existing_index is None:
        missing = REQUIRED_HYPOTHESIS_FIELDS - patch.keys()
        if missing:
            return json.dumps({"error": f"new hypothesis missing required fields: {sorted(missing)}"})
        if patch.get("status", "active") == "active":
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
            "prior_hypothesis_id": None,
            "prior_score": None,
            "extension_used": False,
            "next_step": None,
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
    return json.dumps({"ok": True, "id": patch["id"]})


@tool("evaluate_hypothesis")
def evaluate_hypothesis(hypothesis_id: str) -> str:
    """Compute the real score and verdict for a hypothesis (section 5.3/5.4).
    Counts conversions from signups.jsonl (matched by landing_page_variant_id
    and a submitted_at timestamp inside [created_at, created_at+duration_days])
    and uses whatever measured.reach_estimate is already stored - it does NOT
    guess a reach number. If reach_estimate is still null, returns an error
    saying so instead of scoring; get Growth to call read_channel_metrics and
    write_hypothesis first. Read-only: does not persist anything itself, call
    write_hypothesis afterwards to save status/score/measured.
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

    return json.dumps({
        "hypothesis_id": hypothesis_id,
        "conversions": conversions,
        "estimated_reach": reach,
        "score": score,
        "verdict": scoring.verdict_for_score(score),
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
    - a paid channel (is_paid=true) cannot move to status="testing" without
      approved_request_id pointing at an approval_queue.jsonl entry with
      status="approved" and category="spend" - this reuses the existing
      human approval queue, it does not add a separate spend gate.
    - changing an existing channel's status requires a non-empty `reason`
      (e.g. "reddit averaging -0.4 over 3 evaluated hypotheses, swapping in
      content_marketing") so swaps stay auditable, same as any other
      decision that changes direction.
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
            approved_request_id = record.get("approved_request_id")
            approval = None
            if approved_request_id:
                approvals = _read_jsonl("approval_queue.jsonl")
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
# Cycle notification (orchestration-level, not an agent tool - called
# directly from crew.py after kickoff() so delivery never depends on an
# agent remembering to call it)
# --------------------------------------------------------------------------

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(text: str) -> None:
    """Send a plain-text message via a Telegram bot to a fixed chat, split
    across multiple messages if it exceeds Telegram's 4096-char limit.
    Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment; prints
    a clear warning and returns quietly if either is missing or the send
    fails - a missing/failed notification must never crash the crew run
    that already completed successfully.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - skipping cycle summary notification.")
        return

    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH):
        chunk = text[i:i + TELEGRAM_MAX_MESSAGE_LENGTH]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[telegram] failed to send cycle summary: {exc}")
            return
