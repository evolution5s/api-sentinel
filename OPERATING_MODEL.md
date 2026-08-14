# OPERATING_MODEL.md

> **Maintenance note.** This is a snapshot, not auto-generated, traced
> directly against `crew.py`/`tools.py`/`holding.py`/`scoring.py` as they
> exist on **2026-08-13** (commit range through the duplicate-approval/
> backlog-enforcement addendum). It must be updated alongside any change to
> those four files' task logic or gates - unlike `README.md` (component
> reference, organized by chapter), this document is a sequential account of
> what actually happens, meant to be re-read end to end when auditing
> whether live behavior still matches design. If it disagrees with the real
> code, the code wins; fix this file, not your understanding of the system.

---

## 0. The one fact that changes how you should read everything below

**The live system is currently running on the `testing` agent profile, not
`normal`** (`agent_profile.json`, `active_profile: "testing"`, printed to
stdout on every process start: `agent_profile active: 'testing'
(claude-haiku-4-5)`). Concretely, right now, every cycle:

- All four agents run on **claude-haiku-4-5**, not Sonnet 5 - the profile's
  own description says this is "not for judging output quality," a
  functional smoke-test model, not the production-quality model this
  document's design assumes elsewhere.
- `CYCLE_TOKEN_BUDGET = 250,000`, a quarter of the `normal` profile's
  1,000,000.
- Every agent's `max_iter`/`max_tokens`/`max_execution_time` is the
  `testing` profile's row, not `normal`'s (e.g. Growth: 9 iterations/4,500
  tokens vs. 30 iterations/3,000 tokens with a much longer time budget on
  `normal`).

This is a single line to flip (`active_profile` in `agent_profile.json`,
then redeploy) but nothing in the code enforces switching it back - the
description field says "Switch active_profile back to 'normal' once done"
as a comment, not a mechanical reminder. **Every "verified-live" claim in
section 4 below was, at best, verified live under the `testing` profile,
never yet under `normal`.**

---

## 1. Full cycle walkthrough

One process invocation (`python crew.py`, run by cron - see section 6 for a
cadence discrepancy worth double-checking) does the following, in order.

**Step 0 - before anything else.** `check_state_persistence()` runs first,
deliberately, so a persistence warning reaches Telegram even if everything
after it fails. Outside Railway (no `RAILWAY_ENVIRONMENT_ID`) this is a
no-op (`applicable=False`). Inside Railway, it compares
`RAILWAY_VOLUME_MOUNT_PATH` against `STATE_DIR` - a mismatch means state
will not survive the next redeploy, surfaced as a `persistence_warning` on
every subsequent Telegram message this run, not silently.

**Step 1 - Telegram commands.** `process_telegram_commands()` always runs,
even while paused, so a `start` can still be seen. Fetches updates since
the last processed offset and classifies each into: `stop`/`pause`,
`start`/`resume`/`weiter`, `approve`/`ja`/`yes` or `reject`/`nein`/`no`
(replying to a specific `appr_...` notification), `<id> approve`/`<id>
reject` (typed directly), `posted: <draft_id> <url>`, `removed: <draft_id>
<reason>`, `live: <hypothesis_id>`, `payment_link: <id> <url>`,
`stagnation_ack: <subsidiary_id>`, `fix_resolved: <id>`, `duration_policy:
confirm`/`duration_policy: <4 numbers>`, `fix_thresholds: confirm`/
`fix_thresholds: <5 numbers>`. Never raises - a Telegram/network failure
must never block a cron cycle.

**Step 2 - pause check.** `is_system_paused()` reads durable,
cross-cycle state (a file, not per-run). If paused: sends one short
Telegram notice and the entire cycle stops here - no `crew.kickoff()`, no
per-subsidiary loop at all.

**Step 3 - per-subsidiary loop.** `read_subsidiaries(status="active")`,
falling back to `[{"id": "api-sentinel"}]` if the registry is empty. Today
this is one iteration in practice (the one registered subsidiary). For
each: `tools.set_active_subsidiary(sub_id)` switches which subsidiary's
`STATE_DIR/<id>/` every subsequent tool call resolves against; per-cycle
tracking globals (`_limit_hits`, `_task_usage_log`, `_malformed_tool_calls`,
and now `_pre_dev_task_open_order_ids`, see item 8 below) are cleared so one
subsidiary's numbers never leak into the next one's report within the same
process run.

**Step 4 - pre-kickoff snapshots.** Before `crew.kickoff()`:
`pre_active_count` (currently-active hypothesis count, for the
anti-stagnation check), `cycle_start_counts = snapshot_state_counts()`
(hypotheses/knowledge_base/content_drafts/task_orders/hypothesis_backlog
counts), `pre_pending_idea_count` (holding-level, global - `ideas.jsonl`
isn't part of `snapshot_state_counts`, tracked separately so a cycle that
escalated via `propose_idea` itself isn't double-escalated later).

**Step 5 - `crew.kickoff()`**, `Process.sequential` across five tasks, each
a `ConditionalTask` except the first:

1. **`task_channel_strategy`** (`ceo_agent`, unconditional - nothing spent
   yet). Reads strategic direction and policies, maintains the channel
   roster (Bullseye framework): brainstorm if empty, score, promote/demote
   `testing` slots (cap 3, `MAX_CHANNELS_TESTING`), enforce the paid-channel
   gate (`paid_channels_allowed` policy AND an approved spend request,
   `write_channel` enforces both in that order), total roster cap 20
   (`MAX_TOTAL_CHANNELS`).
2. **`task_growth`** (`growth_agent`, gated by
   `_within_cycle_budget_reserving_ceo_and_main_ceo` - reserves 35% of
   `CYCLE_TOKEN_BUDGET` for the two tasks after it). Processes any open
   `to_role='growth'` task orders, measures reach for active hypotheses via
   `read_channel_metrics`, drafts content (`draft_content`, mechanically
   checked: `rules_checked`+`rules_notes` required every time, no
   AI-tell patterns, per-post_type length caps 600/500/1500 chars,
   `include_product_link` requires `landing_page_live=true`), watches
   `check_community_risk` (≥2 removals in 30 days → `risk="high"`) and
   `get_account_stats` (90/10 genuine-to-promotional ratio target).
3. **`task_ceo`** (`ceo_agent` again, gated by
   `_within_cycle_budget_reserving_main_ceo` - reserves 10% for Main-CEO
   only). The Build-Measure-Learn loop: evaluate due hypotheses
   (`read_due_hypotheses`, itself gated by each hypothesis's own
   `duration_days`/`sample_size_trigger`) via `evaluate_hypothesis` →
   `scoring.compute_score` → `scoring.classify_outcome` (deterministic
   four-way bucket, never agent judgment - see section 2). Every
   `write_hypothesis` call is gated by the evidence-stage ladder (research
   → community_engagement → landing_page → build) and, for any NEW
   promotion to `status='active'`, by `MIN_BACKLOG_BEFORE_ACTIVE_PROMOTION`
   (section 2). Backlog grooming (`write_backlog_candidate`,
   `scoring.compute_ice_score`) happens here when there's real spare
   capacity. The anti-stagnation instruction (prompt-text, reinforced by a
   mechanical post-hoc check - section 2, item 2) lives here too. Ends with
   `set_next_step` for next cycle's report continuity.
4. **`task_main_ceo_review`** (`main_ceo_agent`, gated by
   `_within_cycle_budget`, no further reservation - nothing runs after it
   that needs protecting). Nine numbered steps every cycle: route pending
   ideas (0), review status reports needing a decision (1), decide pivot
   proposals (2), resolve cross-subsidiary requests (3), report holding
   structure (4), `assess_subsidiary_trajectory` health check for every
   active subsidiary (5), Kaizen report assembly - **exactly one
   `file_kaizen_report` call per active subsidiary, every cycle** (5.5,
   merging the Sub-CEO's `kaizen_points` with the Main-CEO's own
   observations), strategic-direction bootstrap for any subsidiary that's
   never had one (6), and (per the newly-added "ground truth over
   assertion" paragraph at the top of this task's description) explicit
   instruction to trust this role's own tool reads over a Sub-CEO's
   free-text narrative rather than pausing the review to adjudicate whether
   already-persisted state is "real."
5. **`task_dev`** (`dev_agent`, gated by `_within_cycle_budget`). Processes
   open `to_role='dev'` task orders: for any tied to a `build` outcome,
   independently verifies `check_approval_status` before opening a PR
   (never trusts another agent's claim); opens PRs via `open_pull_request`,
   never merges. If there were zero open orders before this task ran, the
   report is now mechanically forced to a one-line canonical string
   regardless of what the model wrote (section 2, item 8) - the only place
   in the whole cycle where a report's *text* is overridden by code rather
   than left to the prompt instruction alone.

**Step 6 - after `crew.kickoff()` (success or failure, via `_finish_cycle`,
called from both the `try` and `except` branches so both paths converge on
identical post-processing):**

- `run_fix_checks_for_subsidiary` → `holding.run_fix_checks` (five cheap,
  no-LLM checks - section 2) → for each fired check, one escalated
  `generate_fix_diagnosis` call (a separate, stronger-model LLM call,
  `claude-opus-5`, outside the `agent_profile.json` mechanism on purpose)
  → `holding.append_fix_md` + `holding.record_fix_entry`. This is the
  *only* thing that ever writes to `FIX.md` - it stays empty on the volume
  whenever no check has fired recently, or after a human's `fix_resolved:
  <id>` Telegram reply archives a section back out. An empty `FIX.md` is
  not evidence the mechanism is broken; it can equally mean nothing has
  fired since the last archival.
- `scoring.spare_capacity_produced_nothing(pre_active_count,
  tools.MAX_ACTIVE_HYPOTHESES, cycle_start_counts, snapshot_state_counts())`
  - true only if there was unused active-testing capacity AND every tracked
  count is byte-for-byte identical start to end.
- If that fired AND no new pending idea was filed this cycle (comparing
  `pre_pending_idea_count`/post-count), `_auto_escalate_spare_capacity`
  mechanically files a `propose_idea` fallback in code - the safe last
  resort only; see section 2, item 2 for why the two higher-preference
  actions (pull a backlog candidate into active testing, advance backlog
  grooming) are deliberately left to the prompt instruction and not
  auto-executed.
- `send_cycle_summary`: two sequential Telegram messages. Message A is a
  short technical status (token/cost headline, safety-limit warnings,
  persistence warning). Message B is the business narrative: hypothesis
  overview, top-hypotheses block (active + top backlog, ICE-ranked, see
  section 2), Kaizen business lines, approvals, Main-CEO review summary,
  Dev summary (mechanically overridden per item 8 above when applicable),
  Aufsichtsrat-facing lines (pending stage-skip requests, stagnation
  escalations, FIX.md new entries, reason-less rejections still awaiting a
  real reason - section 2), and the next-step commitment. Both
  messages are split at real section/paragraph/line boundaries (never
  mid-word) if they exceed ~4000 characters, each chunk labeled `(i/n)`
  when there's more than one (section 2, item 5).

---

## 2. Every mechanical gate/rule in the system

| Gate | Threshold (current value) | Enforced at | Kind |
|---|---|---|---|
| Max active hypotheses (WIP cap) | `MAX_ACTIVE_HYPOTHESES = 3` | `tools.py:102`, checked in `write_hypothesis` | Hard block |
| Min scored backlog before active promotion | `MIN_BACKLOG_BEFORE_ACTIVE_PROMOTION = 10` (Jan's explicit mandate) | `tools.py:112`, checked in `write_hypothesis` on any transition INTO `status='active'` | Hard block |
| Evidence-stage ladder | research → community_engagement → landing_page → build | `tools.py` `write_hypothesis` (required fields vary by stage; `STAGE_GATED_ECONOMICS_FIELDS` only required from `landing_page` on) | Hard block (or human-approved `file_stage_skip_request` bypass) |
| Research finding min length | `RESEARCH_FINDING_MIN_LENGTH = 80` chars | `tools.py:131`, `log_research_finding` | Hard block |
| Research finding real URL | must contain `https?://` or start with `"kein Link:"` | `tools.py`, `log_research_finding` (item 6, this addendum) | Hard block |
| Backlog statement min length | `BACKLOG_STATEMENT_MIN_LENGTH = 60` | `tools.py:173` | Hard block |
| ICE scoring range | 1-10 each factor, `impact*confidence*ease` | `scoring.compute_ice_score` | Hard block (`ValueError` outside range) |
| Simple build-cost ceiling | `SIMPLE_BUILD_COST_CEILING = 10.0` (USD) | `tools.py:152` - above this, `BUILD_COST_JUSTIFICATION_MIN_LENGTH = 80` chars of reasoning required | Hard block above ceiling without justification |
| Pivot attempt cap | `scoring.PIVOT_ATTEMPT_CAP = 2` | `scoring.classify_outcome` | Hard, deterministic (never agent judgment) |
| Channel roster caps | `MAX_CHANNELS_TESTING = 3`, `MAX_TOTAL_CHANNELS = 20` | `tools.py:155-156`, `write_channel` | Hard block |
| Paid channel gate | policy `paid_channels_allowed` (default `False`) AND an approved spend request | `write_channel` | Hard block, two-part AND |
| Publish approval dedup, layer a (pending queue) | same `hypothesis_id`+`platform`, similarity ≥ `PUBLISH_DEDUP_SIMILARITY_THRESHOLD = 0.85`, still pending or decided within `PUBLISH_DEDUP_RECENT_DECISION_HOURS = 24`h | `tools.py`, `_find_duplicate_publish_approval` / `request_approval` | Hard block (skipped, not queued) |
| Publish approval dedup, layer b (posting history, same forum) | same similarity threshold against `content_drafts.jsonl` status=`posted`, entire history, same platform+community | `tools.py`, `_find_similar_posted_content` / `request_approval` (item 1b, this addendum) | Hard block |
| Publish approval dedup, layer b (posting history, cross-forum) | same as above, different community | `request_approval` | **Advisory only** - attached as `similar_prior_posts` on the record, rendered in the Telegram notification, never blocks filing |
| Content style checks | no markdown headers/bullets, no "in conclusion"/"in summary"/"as an AI"/"I hope this helps", per-post_type length cap (600/500/1500) | `tools.py`, `draft_content` (`_find_style_violations`, `CONTENT_LENGTH_CAPS`) | Hard block |
| Community risk cooldown signal | ≥2 removals in 30 days → `risk="high"` | `tools.py`, `check_community_risk` | **Advisory only** - surfaced, never blocks a new draft |
| Cycle token budget | `CYCLE_TOKEN_BUDGET` = 250,000 (`testing` profile, **currently live**) / 1,000,000 (`normal`) | `crew.py`, `_within_cycle_budget` family | Hard block (skips remaining tasks this cycle) |
| Budget reservation for CEO+Main-CEO | `RESERVE_FRACTION_FOR_CEO_AND_MAIN_CEO = 0.35` | `crew.py:838` | Hard, gates `task_growth`'s own start |
| Budget reservation for Main-CEO only | `RESERVE_FRACTION_FOR_MAIN_CEO = 0.10` | `crew.py:839` | Hard, gates `task_ceo`'s own start |
| Per-agent max_iter/max_tokens/max_execution_time | see `agent_profile.json`, varies by profile and agent | `crew.py` `Agent(...)` construction | Hard block (crewai-level) |
| Stall detection | `STALL_RESOLVED_THRESHOLD = 5` resolved hypotheses, 0 builds | `holding.py:687`, `assess_subsidiary_trajectory` | Advisory (surfaced in Main-CEO's own report) |
| Stagnation escalation | `STAGNATION_ESCALATION_THRESHOLD = 6` consecutive stalled cycles | `holding.py:702` | Escalation (persists `stagnation_escalated=true`, surfaced every cycle until human `stagnation_ack:` or a real build) |
| Zero-state streak (FIX.md) | 3 consecutive cycles, no new persisted state | `holding.DEFAULT_PROPOSED_FIX_THRESHOLDS["zero_state_streak_cycles"]` | **Proposed, but already active by design** ("proposed" means "confirm or adjust," not "inert") - fires an `opus-5` diagnosis, writes `FIX.md` only, never blocks |
| Recurring malformed tool calls (FIX.md) | 3 consecutive cycles, same signature | `DEFAULT_PROPOSED_FIX_THRESHOLDS["malformed_tool_calls_cycles"]` | Diagnostic write only |
| Channel bury streak (FIX.md) | 3 consecutive buries on one channel | `DEFAULT_PROPOSED_FIX_THRESHOLDS["channel_bury_streak"]` | Diagnostic write only |
| Repeated pivot streak (FIX.md) | 2 | `DEFAULT_PROPOSED_FIX_THRESHOLDS["repeated_pivot_streak"]` | Diagnostic write only |
| Stale approval (FIX.md) | 48h pending | `DEFAULT_PROPOSED_FIX_THRESHOLDS["stale_approval_hours"]` | Diagnostic write only |
| Duration caps per evidence stage | research=3d, community_engagement=5d, landing_page=14d, build=None (`DEFAULT_PROPOSED_DURATION_CAPS`) | `tools.py:209` | **Proposed, NOT yet enforced** until a human confirms via `duration_policy: confirm` |
| Anti-stagnation (spare capacity produced nothing) | active count < WIP cap AND `snapshot_state_counts()` identical start/end | `scoring.spare_capacity_produced_nothing` | Mechanical detection (always fires the finding); the *response* is prompt-text preferred + a code-level last-resort fallback (item 2, this addendum) |
| Dev "no open orders" one-liner | pre-task snapshot of open `to_role='dev'` orders is empty | `crew.py`, `_enforce_dev_one_liner` (item 8, this addendum) | **Hard, mechanical override of the report text itself** - the only gate in the system that rewrites a report's prose rather than blocking/flagging a tool call |
| Telegram message length | split at ~4000 chars (`TELEGRAM_SAFE_CHUNK_LENGTH`), real Telegram hard limit 4096 (`TELEGRAM_MAX_MESSAGE_LENGTH`) | `tools.py`, `_split_message_at_boundaries` / `send_telegram_message` (item 5, this addendum) | Hard (never silently truncates; splits at a real boundary instead) |
| Task-summary render budget | `TASK_SUMMARY_MAX_CHARS = 6000` | `crew.py`, `_task_summary` | Soft (marks visibly `[... gekuerzt ...]` if a genuine outlier still exceeds it; real overflow beyond this is handled by the Telegram splitter above, never a silent cut) |
| Instruction-echo rejection | reasoning fields must not reuse known instruction/incident template phrasing | `tools.py`, `_INSTRUCTION_ECHO_PHRASES` check, applied to `build_cost_reasoning`/`defensibility_notes`/research summaries/stage-skip reasoning | Hard block |
| Defensibility grounding | from `evidence_stage='landing_page'` on, `defensibility_notes`+`defensibility_grounding` both required; `defensibility_grounding` must be a real id (research_finding/knowledge_base/channel/approval) | `tools.py`, `write_hypothesis` (`STAGE_GATED_ECONOMICS_FIELDS`, `_backlog_grounding_exists` reuse) | Hard block |
| Competitor scan staleness | `COMPETITOR_SCAN_STALENESS_DAYS = 90`, per-hypothesis cache freshness for a `topic='competitor scan'` knowledge_base entry | `tools.py:COMPETITOR_SCAN_STALENESS_DAYS`, checked in `task_ceo`'s own prompt text (not mechanically enforced - same as `PAYMENT_PROPENSITY_STALENESS_DAYS`, a cache-freshness knob, not a governance parameter) | Advisory (prompt-level cache check only) |
| Rejection reason requirement | `category` rejection (any category) without a real, non-empty reason does not close the request - stays `status='pending'`, `needs_rejection_reason=true` | `approve.py`, `decide()`; same enforcement reused by the Telegram reject path (`tools.py`, `_apply_telegram_commands`) | Hard block on closing (the reject action itself is always accepted, just doesn't take effect without a reason) |
| State-wipe confirmation | `wipe_confirm:` only proceeds against a matching, unexpired (`WIPE_CONFIRMATION_EXPIRY_HOURS = 24`) pending wipe from a prior `wipe_state:` | `tools.py`, `execute_confirmed_wipe`/`prepare_state_wipe` | Hard block - human-only, Telegram-gated, no `@tool`, unreachable by any agent |
| Stale strategic-direction detection | regex-only match on hardcoded `$`-figures/`N+ conversions` patterns in `focus_area`, no LLM judgment | `holding.py`, `is_stale_strategic_direction`; forced correction via `crew.maybe_correct_stale_strategic_direction`, run in `__main__` before `crew.kickoff()` | Hard, mechanical override - confirmed necessary after Main-CEO judgment missed it twice live |
| 10-backlog-rule gate, no exception | `len(scored candidates) >= MIN_BACKLOG_BEFORE_ACTIVE_PROMOTION (10)` required for ANY hypothesis-tied progression, active or not - grandfather clause fully removed | `tools.py`, `_backlog_gate_satisfied`/`_scored_backlog_count`, checked in `write_hypothesis` (any `effective_status=='active'`), `file_task_order` (any `hypothesis_id`), `request_approval(category='publish')` (template `hypothesis_id`) | Hard block - only a genuinely resolving `write_hypothesis` update (`status='evaluated'/'buried'`) or a hypothesis-order-less/non-publish action gets through |
| Approval withdrawal | `withdraw_approval` only touches `status='pending'` entries, requires a real reason | `tools.py`, `withdraw_approval` (real `@tool`, on `ceo_agent`) | Hard block on already-decided entries |

---

## 3. Agent-boundary reference

**Tier 0 (fully autonomous, no human sign-off):** everything not listed
under Tier 1/2 below - reading state, drafting content (not posting),
scoring hypotheses, filing task orders between agents, backlog grooming,
channel roster management within policy, research/community-engagement
evidence-stage work.

**Tier 1 (needs the Main-CEO, not the human board):** pivot proposals
(`file_pivot_proposal` → `decide_pivot_proposal`), cross-subsidiary
requests, stage-skip requests (`file_stage_skip_request` →
`decide_stage_skip_request`), idea routing (`propose_idea`/`read_ideas`
→ `route_idea`), Kaizen `fuer_aufsichtsrat` items that are policy-level but
not yet spend/legal/publish/deploy/pricing.

**Tier 2 (needs the human board, via `request_approval` +
`approve.py`/Telegram):** `category ∈ {spend, legal, publish, deploy,
pricing}` - the one queue, `approval_queue.jsonl`, never executed by the
system itself. A brand-new subsidiary spin-off also always needs a
`request_approval` before `register_subsidiary` can act, even though the
routing decision itself is Tier 1.

**Per-agent tool access** (from `crew.py`'s `Agent(tools=[...])`
constructions, verified against the actual list, not assumed):

- **`growth_agent`**: `request_approval`, `read_channel_metrics`,
  `read_channels`, `read_state`, `read_hypotheses`, `read_task_orders`,
  `complete_task_order`, `draft_content`, `read_content_drafts`,
  `check_community_risk`, `get_account_stats`, `log_research_finding`,
  `read_research_findings`, `read_subsidiary_policies`,
  `read_knowledge_base`, `propose_idea`, `write_backlog_candidate`,
  `search_web`, `read_webpage`. No `write_knowledge_entry`, no
  `write_hypothesis`, no `open_pull_request` - Growth measures and drafts,
  never writes hypotheses or ships code.
- **`dev_agent`**: `open_pull_request`, `read_task_orders`,
  `complete_task_order`, `check_approval_status`, `write_backlog_candidate`.
  The smallest tool list of the four, matching the smallest role (ship a PR
  for an already-approved order, or say why not).
- **`ceo_agent`**: the largest list - `read_state`, `read_hypotheses`,
  `read_due_hypotheses`, `write_hypothesis`, `evaluate_hypothesis`,
  `check_escalation`, `compare_channel_performance`, `request_approval`,
  `read_channels`, `write_channel`, `compute_break_even`, `file_task_order`,
  `read_task_orders`, `file_status_report`, `read_strategic_direction`,
  `file_pivot_proposal`, `file_cross_subsidiary_request`,
  `search_research_archive`, `read_subsidiary_policies`,
  `read_content_drafts`, `log_research_finding`, `read_research_findings`,
  `read_knowledge_base`, `write_knowledge_entry`, `propose_idea`,
  `file_stage_skip_request`, `write_backlog_candidate`, `read_backlog`,
  `set_next_step`, `search_web`, `read_webpage`. **Does not have
  `complete_task_order`** - see section 5, finding 1, this is the confirmed
  prior dead-instruction bug and it's still the case that only Growth/Dev
  hold that tool, which is correct (only they execute orders); the Sub-CEO
  only ever *files* them.
- **`main_ceo_agent`**: `read_subsidiaries`, `register_subsidiary`,
  `set_subsidiary_status`, `read_pivot_proposals`, `decide_pivot_proposal`,
  `read_cross_subsidiary_requests`, `resolve_cross_subsidiary_request`,
  `read_status_reports`, `acknowledge_status_report`,
  `set_strategic_direction`, `read_strategic_direction`,
  `assess_subsidiary_trajectory`, `search_research_archive`,
  `request_approval`, `read_subsidiary_policies`,
  `update_subsidiary_policies`, `propose_idea`, `read_ideas`, `route_idea`,
  `read_stage_skip_requests`, `decide_stage_skip_request`,
  `write_backlog_candidate`, `read_backlog`, `search_web`, `read_webpage`,
  `file_kaizen_report`.

---

## 4. Verified-live vs. implemented-but-unconfirmed

Read literally: "verified-live" means direct evidence from a real,
completed `crew.kickoff()` cycle (a log excerpt, an actual persisted
record) was available to whoever last updated this section. Everything
else is "implemented, tested in isolation, not yet confirmed live" - that
is not a criticism, most of this system's mechanics are only cheaply
testable that way, but the distinction matters for trust calibration.

| Mechanism | Status | Basis |
|---|---|---|
| Core Build-Measure-Learn loop (`write_hypothesis`/`evaluate_hypothesis`) | **Verified-live** | Existing `hyp_research_001` is a real, live-created active hypothesis referenced throughout prior addenda and README |
| Evidence-stage ladder | **Verified-live** (research/community_engagement stages) | Real drafted/posted content and research findings referenced in prior session's confirmed production data (6+ near-duplicate publish approvals for `hyp_research_001`, per this addendum's own item 1 brief) |
| Duplicate publish approvals existing in production | **Verified-live** (this is the exact bug item 1 fixes) | Explicitly confirmed by the project owner in this addendum's own text, not something this session observed directly |
| FIX.md mechanism, Kaizen, payment-propensity scan | **Implemented, not confirmed live this session** | Code + `checkup.py` coverage exist; `FIX.md` was found empty on the Railway volume during this session's live-audit attempt (see section 6) - consistent with "nothing fired since the last archival," not proof of failure, but not positive confirmation either |
| Hypothesis backlog / ICE scoring | **Implemented, not confirmed live** | `MIN_BACKLOG_BEFORE_ACTIVE_PROMOTION` gate is brand-new this addendum; no live cycle has run against it yet |
| Anti-stagnation mechanical fallback (`_auto_escalate_spare_capacity`) | **Implemented, not confirmed live** | Added this addendum; only exercised via `checkup.py` |
| Publish-history dedup (item 1b, cross-forum flag) | **Implemented, not confirmed live** | Added this session; no live `content_drafts.jsonl` entry has yet triggered it in production |
| Telegram message splitting at real boundaries | **Implemented, not confirmed live** | The bug it fixes (mid-word truncation) *was* confirmed live in a real cycle; the fix itself has only run under `checkup.py` |
| Dev one-line mechanical enforcement | **Implemented, not confirmed live** | Brand-new this addendum |
| Main-CEO stage-skip review (step 6.5) | **Implemented, not confirmed live** | Brand-new this addendum (section 5, finding 4) - closes a real gap where pending stage-skip requests had no operational step ever reading/deciding them |
| Competitor scan + defensibility grounding | **Implemented, real live network test (not production)** | Full real pipeline (`search_web`/`read_webpage`/`log_research_finding`/`draft_content`/`write_knowledge_entry`/`write_hypothesis`) run against real Serper results and `hyp_research_001`'s real, documented topic domain, but against a local throwaway `STATE_DIR` - production `hyp_research_001` itself was not touched (blocked, see section 6) |
| Rejection-reason enforcement | **Implemented, not confirmed live** | Brand-new this addendum; `checkup.py` covers both the `approve.py` and Telegram paths, no real cycle has exercised it yet |
| Full state wipe (`wipe_state:`/`wipe_confirm:`) | **Implemented, deliberately not yet triggered on production** | Item 6 of this addendum, sequenced explicitly after items 1-3 above were confirmed fixed. `checkup.py` covers both phases, subsidiary isolation, expiry, and the full Telegram round-trip against real (test) state. Execution against the real `api-sentinel` production state is Jan's own call, via a real `wipe_state:`/`wipe_confirm:` Telegram exchange whenever he's ready - not something this session executed unilaterally |
| Task-main-ceo-review "ground truth over assertion" fix | **Implemented, not confirmed live** | The regression it fixes (Main-CEO second-guessing persisted state) was confirmed live; the fix has not yet run a real cycle |
| `agent_profile.json` `testing` mode | **Verified-live, currently active** | Printed directly to stdout on every process start; confirmed by this session's own test runs |
| `update_reach_multiplier` recalibration | **Not live, not reachable by any agent at all** | See section 5, finding 5 |
| Stale-direction mechanical override | **Confirmed real-live problem, fix implemented, live verification pending** | Real `dir_ccfb394d` on production twice survived Main-CEO judgment across two separate real cycles - the failure mode itself is confirmed live; the mechanical fix has only run under `checkup.py` so far, pending a live cycle to confirm it actually replaces the real direction |
| 10-backlog-rule gate (no exception) + `hyp_research_001` backlog conversion | **Gate mechanism implemented and tested; `hyp_research_001`'s conversion into a scored backlog candidate and the withdrawal of its two real pending publish approvals (`appr_52dbb9d5`, `appr_018963dc`) require live production access, not yet executed** | See section 6 for why live access is currently gated behind an explicit manual trigger or a natural cron window |

---

## 5. Inactive/dead code audit

Verified directly by tracing tool lists, imports, and field usage - not
guessed. Broader automated sweep (unused JSONL fields, superseded paths)
was additionally delegated to a research pass over the same codebase in
parallel with writing this document; if that surfaced further findings
they are appended as a dated addendum below this section rather than
silently merged in, so this document's authorship stays traceable.

1. **`scoring.update_reach_multiplier` is completely unreachable by any
   agent.** It is not decorated with `@tool`, not imported into `crew.py`'s
   `from tools import (...)` block (it lives in `scoring.py`, which
   `crew.py` imports as a module but only calls `scoring.compute_score`,
   `scoring.classify_outcome`, `scoring.compute_ice_score`,
   `scoring.spare_capacity_produced_nothing` directly - never
   `update_reach_multiplier`), not wired to any agent's `tools=[...]`. Its
   own docstring says "Nothing calls this automatically today - judging
   when 'enough' data points exist is left to the CEO agent's discretion" -
   but `ceo_agent` has no tool that reaches it at all, so that discretion
   is currently impossible to exercise. README references it twice as
   something that "should be calibrated via `update_reach_multiplier`."
   **Recommendation: either expose it as a real `@tool` on `ceo_agent`
   (matching the docstring's stated intent), or update the docstring/README
   to say plainly that recalibration is currently a manual, out-of-band
   operation (e.g. editing `reach_estimators.json` directly), not something
   any agent can do today.**
2. **`ceo_agent` still does not have `complete_task_order`** (confirmed via
   its exact `tools=[...]` list, section 3). This is not a new bug - it's
   the same, already-fixed-elsewhere shape as the confirmed prior
   free-text-bury/`complete_task_order` incident this addendum's own brief
   cites as the template finding. Checked directly: no current task
   description tells `ceo_agent` to call `complete_task_order` on itself
   (only Growth/Dev's own task descriptions reference it, and only
   Growth/Dev hold the tool) - **this one is correctly wired, not a
   regression, included here only to record that it was actually checked,
   not assumed safe.**
3. **`item 4` of the pasted addendum text (task_main_ceo_review fix) was
   never numbered "item 4" anywhere in this codebase's commit history**
   before this document (see this addendum's own git log - items 1,2,3,5,6
   appear in comments across `tools.py`/`crew.py`, item 4 does not,
   confirmed by grepping every recent commit). The underlying work (the
   "ground truth over assertion" paragraph in `task_main_ceo_review`) does
   exist and matches what a fix for that regression would look like - it
   most likely was done under a different or no explicit numbering, not
   skipped. Not a code defect, but worth a human confirming the addendum's
   original item 4 text is/was fully captured, since this document's author
   could not recover the original numbered list.

### 2026-08-13 addendum - deeper automated sweep

A follow-up pass cross-referenced every agent's exact `tools=[...]` list
against that same agent's own backstory and task-description text (not
other agents'), every `@tool`-decorated function's reachability from
`crew.py`, and every JSONL field written but never read back. Findings
below, most-actionable first; the tool-list ones were left as
documentation rather than applied live, since removing a tool from a
running agent is a real behavior change this document's author could not
validate against an actual cycle.

4. **Confirmed real functional gap, now fixed in this same pass:**
   `main_ceo_agent` has held `read_stage_skip_requests`/
   `decide_stage_skip_request` in its `tools=[...]` since the
   structural-rebuild addendum, and its own backstory describes the
   responsibility (`crew.py:648-649`) - but `task_main_ceo_review`'s actual
   numbered checklist (steps 0-8) never called either tool. `ceo_agent`
   genuinely does file these (`file_stage_skip_request`, `task_ceo` step on
   evidence-stage skips), so a pending stage-skip request could sit in
   `stage_skip_requests.jsonl` indefinitely with no operational step ever
   telling the Main-CEO to go look. This is the same bug shape as the
   confirmed prior `complete_task_order` incident (a tool granted, a
   backstory describing intent, but no task step that actually calls it) -
   **fixed directly in this pass**: added step 6.5 to
   `task_main_ceo_review` (`crew.py`, right after the strategic-direction
   step) instructing it to read pending stage-skip requests every cycle and
   decide each, pushing back by default. Not yet confirmed live (see
   section 4).
5. **Tools granted but never referenced in that agent's own prompt text**
   (backstory + task description + expected_output) - documented, not
   removed, pending a human decision on wire-in vs. remove:
   - `growth_agent`: `read_research_findings`, `read_knowledge_base`
     (`crew.py:252-253`) - zero mentions in `growth_agent`'s backstory or
     `task_growth`. Growth only ever *writes* research findings
     (`log_research_finding`); nothing tells it to read either back.
   - `ceo_agent`: `read_state` (`crew.py:529`), `read_task_orders`
     (`crew.py:532`), `read_content_drafts` (`crew.py:535`) - all three are
     genuinely used, but by `growth_agent`'s own task, not `ceo_agent`'s.
     Reads as a likely copy/paste artifact from assembling the tool lists.
     `read_task_orders` in particular is a real, if minor, missed
     opportunity: nothing currently stops `ceo_agent` from re-filing a
     duplicate open order for the same hypothesis, since it never reads its
     own filed orders back.
   - `main_ceo_agent`: `search_web`, `read_webpage` (`crew.py:697`) - not
     mentioned in `task_main_ceo_review`'s 9 steps; these are exercised by
     `growth_agent`/`ceo_agent` per their own task text instead.
   - `main_ceo_agent`: `read_subsidiary_policies`/`update_subsidiary_policies`,
     `write_backlog_candidate`/`read_backlog` - backstory-only, absent from
     the numbered checklist (weaker case than the stage-skip one above,
     since nothing suggests these currently pile up unprocessed the way
     stage-skip requests did - plausibly genuinely rare/on-demand tools).
   - `dev_agent`: `write_backlog_candidate` (`crew.py:287`) - mentioned only
     in `dev_agent`'s own backstory (`crew.py:282`), never in `task_dev`'s
     description. A one-line addition mirroring `growth_agent`'s existing
     instruction would close this cheaply if it's judged worth doing.
6. **`file_task_order`'s `from_role` field is dead.** Hardcoded to the
   literal `"sub_ceo"` on every write (`tools.py`); grepped across the
   whole codebase, it's never read, filtered, or branched on anywhere, and
   since only `ceo_agent` holds `file_task_order`, the value could never
   have been anything else. Safe to remove.
7. **`business_reports.jsonl`'s `id` field is currently write-only** -
   unlike every other id type in this system (order ids, approval ids,
   hypothesis ids), nothing ever looks a business report up by id;
   `read_last_business_report()` only returns the most recent record
   positionally. Plausibly reserved for a not-yet-built id-based lookup
   rather than a bug - flagged, not removed.
8. **`sync_signups_from_github`'s parsed `email`/`tier`/`consent` fields
   are never read back** by any agent or scoring path (only
   `landing_page_variant_id`/`submitted_at`/`issue_number` are). Read as an
   intentional human-audit trail (GDPR-adjacent, meant for a person reading
   the raw JSONL) rather than a bug - but `tier` specifically looks like it
   was meant to eventually feed pricing-tier/economics decisions and
   currently doesn't connect to any such path. Worth a human confirming
   that's still the intent.
9. **The confirmed prior `complete_task_order`/bury-instruction incident
   this addendum's own brief cites as the template bug has already been
   fixed**, and correctly: `write_hypothesis` now mechanically auto-closes
   any open task order tied to a buried hypothesis (`tools.py`) instead of
   relying on a free-text instruction telling `ceo_agent` to call a tool it
   never had. No action needed - cited here only as the confirmed
   historical precedent for findings 4-5 above, not as an open item.

---

## 6. Known gaps and limitations (reframed from README chapter 15)

- **Cron cadence discrepancy - resolved 2026-08-13, real cadence is 2h.**
  `tools.py`'s `is_system_paused`/`sync_signups_from_github` docstrings
  used to say "6h," and `railway status --json` showed two different
  values in two different fields of the same response
  (`"0 */6 * * *"` at the service-instance level vs. `"0 */2 * * *"` in
  the `fileServiceManifest`/`serviceManifest` deployment metadata, which
  is sourced from the repo's own `railway.json`). Resolved with real
  evidence, not just config-reading: `railway logs --deployment <old,
  long-lived deployment id> --json`, filtered to `"Autonomous Loop
  Started"` lines, gave four consecutive real container-start timestamps
  from 2026-08-11 (system was paused that whole stretch via Telegram
  `stop`, so the loop started but did no real work each time - a clean
  signal): `10:04:53`, `12:02:15`, `14:02:58`, `16:03:05`, `18:02:55` -
  gaps of 1h57m, 2h00m, 2h00m, 1h59m, i.e. genuinely ~2h, matching
  `railway.json` and refuting the "6h" docstrings. `deployment list`
  itself could NOT be used for this - it only records build/deploy
  events (every entry in a normal session shows `"reason": "deploy"`,
  triggered by a push), never a cron tick re-running an already-deployed
  image, so it undercounts real executions completely; real container-
  start log timestamps were the only reliable signal. Both `tools.py`
  docstrings corrected to "2h" with this evidence cited inline. The
  stray `"0 */6 * * *"` service-instance-level field itself is still
  unexplained (likely a stale Railway dashboard setting independent of
  `railway.json` that was never actually enforced) but is now confirmed
  cosmetic, not something driving real behavior - not worth further
  chasing unless it starts disagreeing with real timestamps again.
- **No CLI path currently gives real volume access between cron ticks -
  confirmed empirically, three ways tried, all three fail** (session of
  2026-08-13). `railway run -- cat /data/_holding/FIX.md` runs *locally*
  with the linked environment's variables injected, not remotely - on a
  machine without an actual `/data` mount it fails locally, not because
  `FIX.md` doesn't exist on the volume. `railway ssh` fails with an
  explicit `"Your service's container is not running (status: created)"` -
  this is a cron job, which by design has no running container between
  scheduled executions (confirmed against Railway's own cron-jobs docs:
  "expected to execute a task, and terminate as soon as that task is
  finished, leaving no open resources"). `railway service files list
  /list` / `download` was tried specifically because it looked like it
  might read the volume's persistent storage directly rather than the
  running container - it does not: it fails with `Failed to initialize
  SFTP session / Timeout`, the same underlying "needs a live container"
  mechanism as `ssh`, just a less legible error message. **`CLAUDE.md` has
  been corrected** to state this plainly rather than the previous (wrong)
  claim that `railway run -- cat` "liefert den exakten Pfad." Real access
  is currently only possible (a) during an actual scheduled cron execution
  window (`ssh`/`service files` while the container is briefly up), or (b)
  by deliberately triggering an extra, out-of-cycle run via `railway
  restart`/`railway redeploy` (or the dashboard's Restart/Redeploy button,
  or Cmd+K "Deploy Latest Commit"). Checked against Railway's own docs:
  there is no separate "start the container without running the start
  command" trigger for a cron-configured service - restart/redeploy runs
  the real `python crew.py` entrypoint, i.e. a genuine, complete,
  out-of-schedule agent cycle with real Anthropic API cost and real side
  effects (Telegram messages, possible approval-queue writes), not a
  free/side-effect-free way to get an SSH window. **Decision (2026-08-13,
  Jan):** do not convert to an always-on worker - the permanent
  continuous-billing cost is disproportionate to how rarely live volume
  access is actually needed.

  **Update, same day, two-step correction:** the manual restart/redeploy
  trigger was initially documented as "usable after explicit sign-off each
  time." Turned out `.claude/settings.json`'s existing `deny` entries for
  `railway service restart`/`railway service redeploy` did not cover the
  functionally identical top-level `railway restart`/`railway redeploy`
  aliases - a real gap, not a theoretical one (this session's
  `settings.local.json` showed the top-level form had already been
  separately approved once). Logged to `PERMISSION_REQUESTS.md`; Jan asked
  for an immediate fix rather than deferring to a batch review, so all
  four spellings were added to `deny` - which overshot: a `deny` rule
  can't be bypassed by asking, so the trigger became entirely unusable
  instead of just gated. **Corrected immediately after:** all four
  spellings removed from `deny` again and left deliberately unlisted -
  same category as `railway ssh`, neither blanket-allowed nor
  blanket-denied, a real per-use confirmation prompt every time. Full
  back-and-forth in `PERMISSION_REQUESTS_reviewed_2026-08-13.md`. Genuine
  live volume access still requires either catching an actual scheduled
  cron window, or a confirmed one-off use of the restart/redeploy trigger.
- **`testing` agent profile is live in production** (section 0) - every
  cycle currently runs on Haiku with a quarter of the intended token
  budget. Nothing mechanically reminds anyone to switch back to `normal`.
- **One subsidiary in practice, but the known hardcoded-content gap is
  fixed (2026-08-13, competitor-research addendum).** The holding model
  supports many, but every shared agent/task text was audited for
  concrete `api-sentinel`-specific content (channel names, audience
  descriptions, problem framing) that would have silently carried over to
  a second subsidiary's first real cycle. Fixed: `task_channel_strategy`'s
  hardcoded r/algotrading-style candidate list (replaced with generic
  brainstorming guidance that derives the real niche from
  `read_strategic_direction`/existing hypotheses at runtime);
  `growth_agent`'s and `ceo_agent`'s backstories (Freqtrade/CCXT-specific
  framing); `ceo_agent`'s `role`/`goal` (hardcoded "API Sentinel" - now
  `{subsidiary_id}`, confirmed via crewai's own `agent.interpolate_inputs`
  to re-derive correctly per subsidiary from a cached original template,
  not a one-way destructive replace); the payment-propensity scan's
  example evidence list (trading-bot-specific categories); the Main-CEO's
  strategic-direction-baseline example (a "Freqtrade/CCXT users" phrasing
  inside a task that explicitly runs once per active subsidiary, so a
  hardcoded example there was especially wrong); and the pause-skip
  Telegram message (hardcoded "API Sentinel" for what's actually a
  holding-wide flag, checked once before the per-subsidiary loop even
  starts). `checkup.py` now has a regression test for genuine per-
  subsidiary parametrization and for the specific hardcoded list's
  absence, not just "doesn't crash." What's still genuinely a one-
  subsidiary-only limitation: the *continuity note* content (prior
  cycle's free-text business-report digest) is inherently per-subsidiary
  dynamic state, not hardcoded text - no fix needed there, just noted so
  it isn't confused with the fixed gap above.
- **Duration caps are proposed, not enforced**, until a human explicitly
  confirms via `duration_policy: confirm` - a hypothesis can currently run
  past what looks like a firm per-stage cap because that cap has never been
  actively turned on.
- **FIX.md diagnostic thresholds are "proposed but already active"** - a
  subtler state than either fully off or fully confirmed; worth
  understanding precisely before assuming "proposed" means "not yet doing
  anything" (it is).
