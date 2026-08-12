import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from crewai import Agent, Crew, Process, Task, LLM
from crewai.events import crewai_event_bus
from crewai.events.types.tool_usage_events import ToolValidateInputErrorEvent
from crewai.tasks.conditional_task import ConditionalTask

import crewai_patches
import holding
import pricing
import scoring
import tools

# Muss vor jeder Agent/Task-Konstruktion passieren, insbesondere vor dem
# ersten crew.kickoff() - siehe crewai_patches.py: crewai wirft einen
# "assistant message prefill"-400 (Anthropic lehnt jede Conversation ab,
# die auf einer assistant-Nachricht endet) IMMER, wenn ein Agent sein
# max_iter tatsaechlich erreicht - kein Rand-, sondern der Normalfall,
# sobald ein Cap wirklich mal greift. Reproduziert in Produktion.
crewai_patches.apply_patches()

from tools import (
    build_hypothesis_overview,
    build_top_hypotheses_block,
    check_approval_status,
    check_community_risk,
    check_escalation,
    check_state_persistence,
    compare_channel_performance,
    complete_task_order,
    compute_break_even,
    draft_content,
    evaluate_hypothesis,
    file_task_order,
    get_account_stats,
    get_and_clear_pending_next_step,
    is_system_paused,
    list_pending_approval_ids,
    log_cycle_usage,
    log_research_finding,
    notify_new_pending_approvals,
    open_pull_request,
    process_telegram_commands,
    read_backlog,
    read_channel_metrics,
    read_channels,
    read_content_drafts,
    read_due_hypotheses,
    read_hypotheses,
    read_knowledge_base,
    read_last_business_report,
    read_last_cycle_note,
    read_research_findings,
    read_state,
    read_task_orders,
    read_webpage,
    request_approval,
    save_business_report,
    save_cycle_note,
    search_web,
    send_telegram_message,
    set_next_step,
    snapshot_state_counts,
    write_backlog_candidate,
    write_channel,
    write_hypothesis,
    write_knowledge_entry,
)
from holding import (
    acknowledge_status_report,
    assess_subsidiary_trajectory,
    decide_pivot_proposal,
    decide_stage_skip_request,
    file_cross_subsidiary_request,
    file_kaizen_report,
    file_pivot_proposal,
    file_stage_skip_request,
    file_status_report,
    propose_idea,
    read_cross_subsidiary_requests,
    read_ideas,
    read_kaizen_actions,
    read_kaizen_suggestions,
    read_pivot_proposals,
    read_stage_skip_requests,
    read_status_reports,
    read_strategic_direction,
    read_subsidiaries,
    read_subsidiary_policies,
    register_subsidiary,
    resolve_cross_subsidiary_request,
    route_idea,
    search_research_archive,
    set_strategic_direction,
    set_subsidiary_status,
    update_subsidiary_policies,
)

_previous_cycle_note = read_last_cycle_note()
# cycle-reporting/backlog addendum, Part 1.2: same read-once-at-module-load
# interpolation pattern as _previous_cycle_note above, just for the prior
# business report's structured next-step commitment specifically (the
# comparison task_ceo's own report opens with) instead of the whole prior
# free-text digest.
_previous_business_report = read_last_business_report()
_previous_next_step = (_previous_business_report or {}).get("next_step") or ""

# Anthropic API Key aus den Umgebungsvariablen prüfen
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("[Error] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# --------------------------------------------------------------------------
# Agent-Profil (Modell + Token-/Iterations-/Zeit-Kappen pro Agent) kommt aus
# agent_profile.json statt hier hartcodiert zu sein - siehe die Datei selbst
# fuer die "testing"/"normal"-Profile. Zum Zurueckwechseln auf die normalen
# Einstellungen einfach active_profile in dieser Datei auf "normal" setzen
# und neu deployen - kein Code-Aenderung noetig.
#
# thinking bleibt bewusst fuer ALLE Agenten in JEDEM Profil ungesetzt (kein
# thinking={"type": ...} irgendwo): Sonnet 5 laeuft dann laut Anthropic-Doku
# automatisch adaptiv, Haiku 4.5 (ein aelteres Modell ohne adaptive-thinking-
# Support) laeuft ohne Denken, wenn thinking nicht gesetzt ist - beide sind
# also mit derselben Nicht-Konfiguration korrekt und minimal bedient, kein
# Sonderfall pro Profil noetig. thinking={"type": "disabled"} wurde vor zwei
# Commits bewusst entfernt (siehe Git-Historie) - das war die Ursache des
# "thinking.disabled.budget_tokens: Extra inputs are not permitted"-Fehlers,
# der jeden Cron-Lauf auf Sonnet 5 abgebrochen hat (crewai-Bug: model_dump()
# eines "disabled"-Thinking-Configs liefert IMMER budget_tokens: None mit,
# Anthropic lehnt das Feld unter type="disabled" komplett ab, auch als null).
_AGENT_PROFILE_FILE = Path(__file__).parent / "agent_profile.json"


def _load_agent_profile() -> dict:
    with _AGENT_PROFILE_FILE.open("r", encoding="utf-8") as f:
        config = json.load(f)
    active = config["active_profile"]
    return {"name": active, **config["profiles"][active]}


AGENT_PROFILE = _load_agent_profile()
print(f"[api-sentinel] agent_profile active: '{AGENT_PROFILE['name']}' ({AGENT_PROFILE['model']}) - {AGENT_PROFILE['description']}")

_ANTHROPIC_KWARGS = {"model": f"anthropic/{AGENT_PROFILE['model']}", "api_key": ANTHROPIC_KEY}

growth_llm = LLM(max_tokens=AGENT_PROFILE["agents"]["growth"]["max_tokens"], **_ANTHROPIC_KWARGS)
dev_llm = LLM(max_tokens=AGENT_PROFILE["agents"]["dev"]["max_tokens"], **_ANTHROPIC_KWARGS)
ceo_llm = LLM(max_tokens=AGENT_PROFILE["agents"]["sub_ceo"]["max_tokens"], **_ANTHROPIC_KWARGS)
main_ceo_llm = LLM(max_tokens=AGENT_PROFILE["agents"]["main_ceo"]["max_tokens"], **_ANTHROPIC_KWARGS)

# FIX.md addendum, Part 1.2: the one escalated diagnostic call, deliberately
# on a stronger/different model than the routine per-cycle AGENT_PROFILE
# model above - fires rarely (only when holding.run_fix_checks reports a
# real threshold crossing), so the added cost of a stronger model for that
# one call is bounded. claude-opus-5 (not the addendum's originally-named
# claude-opus-4-8, a now-superseded legacy model at the identical price -
# $5/$6.25/$10/$0.50/$25 per MTok base/5m-write/1h-write/hit/output for
# both, confirmed against Anthropic's pricing docs 2026-08-11, see
# pricing.py) - no reason to reach for the superseded one. Outside the
# _ANTHROPIC_KWARGS profile mechanism on purpose: this call is not one of
# the four routine agents and must never be swapped by an agent_profile.json
# change meant for those.
fix_llm = LLM(model="anthropic/claude-opus-5", api_key=ANTHROPIC_KEY, max_tokens=2000)

# Agents mit dem Claude-LLM konfigurieren.
# Pro Agent gesetzte Kappungen gegen Budget-Ausreisser in einem einzelnen
# Cron-Lauf (verifiziert gegen die tatsaechlich installierte crewai-Version,
# 1.15.9 lokal und 1.15.11 gepinnt, beide geprueft - siehe Agent.model_fields,
# nicht angenommen):
#   - max_iter: bereits vorhanden, unveraendert - harte Obergrenze fuer
#     Tool-Aufrufe/Denkschritte einer einzelnen Task-AUSFUEHRUNG.
#   - max_execution_time (Sekunden): harte Wall-Clock-Grenze pro Task-
#     AUSFUEHRUNG - faengt Faelle ab, in denen wenige, aber sehr lange/teure
#     Schritte (nicht viele kleine) das Budget sprengen wuerden. Ein
#     Ueberschreiten wirft TimeoutError, das crew.kickoff() nach oben
#     durchreicht und vom bestehenden kickoff_error-Pfad in
#     send_cycle_summary() gemeldet wird.
#   - max_rpm: zusaetzliches Sicherheitsnetz gegen einen Agenten, der in
#     kurzer Zeit ungewoehnlich viele Requests abfeuert.
#   - max_retry_limit (neu, explizit statt crewai's Default 2): wenn eine
#     Task mit einem Fehler abbricht, wiederholt crewai sie automatisch -
#     und jeder Retry startet mit VOLLEM max_iter/max_execution_time neu
#     (verifiziert: agent._handle_execution_error() ruft self.execute_task()
#     komplett neu auf). Bei einem deterministisch fehlschlagenden Fehler
#     (wie dem thinking.disabled-Bug, der jeden Lauf abgebrochen hat) heisst
#     das: bis zum 3-fachen des eigentlichen Budgets, bevor endgueltig
#     aufgegeben wird. 1 statt 2 begrenzt das auf hoechstens das Doppelte -
#     genug Toleranz fuer einen echten transienten Fehler (Netzwerk-Hickser),
#     aber kein 3x-Multiplikator mehr bei einem wiederkehrenden Bug.
#
# Was diese Vier NICHT abdecken: eine Grenze ueber den GESAMTEN Zyklus
# (alle 5 Tasks zusammen) - jede der obigen Kappungen wirkt nur pro
# einzelner Task-Ausfuehrung. ceo_agent laeuft zweimal pro Zyklus (Channel-
# Strategie + Build-Measure-Learn), ohne dass sein Budget dafuer irgendwo
# addiert sichtbar waere. Das schliesst CYCLE_TOKEN_BUDGET weiter unten,
# direkt vor den Task-Definitionen (siehe dort).
growth_agent = Agent(
    role="Growth Engine / Dev Relations",
    goal="Draft genuine, organic community content matching the currently active hypothesis, measure real reach after it's approved and posted, and keep every account's posting history within its community's rules and its 90/10 genuine-to-promotional ratio",
    backstory=(
        "Technical marketer for the Freqtrade/CCXT and quant-bot communities. "
        "Drafts real posts (draft_content) in its own plain words - never "
        "publishing them itself, that's always a human, confirmed back via a "
        "Telegram 'posted:' reply. For a 'thread_reply' specifically - "
        "replying inside someone else's real thread - read_webpage(url) on "
        "that actual thread first where practical, so the reply responds "
        "to what was genuinely said there, not a guess at what the thread "
        "probably contains; search_web is available too for finding "
        "related threads/context. Every reach number comes from "
        "read_channel_metrics, never a guess. One account per product per "
        "platform: before drafting for a community, checks that community's "
        "own current rules (rules_checked/rules_notes on every single draft, "
        "not just once per platform) and check_community_risk for that "
        "community's recent post-removal history - repeated removals mean "
        "cool off and rethink the approach there, not try again unchanged. "
        "Watches get_account_stats to keep each platform's content roughly "
        "90% genuinely useful/curious and only 10% promotional on a rolling "
        "basis - a 'own_question_post' asking something real is itself a "
        "valid, non-promotional way to learn (log_research_finding records "
        "what came back from it). Never includes a product link before "
        "read_hypotheses shows landing_page_live=true for that hypothesis, "
        "and prefers a profile/signature link over an inline one even once "
        "it is live. Concrete work comes from the Sub-CEO as task orders "
        "(read_task_orders(to_role='growth', status='open')) - that's the "
        "authoritative ask, not a paraphrase of it; call complete_task_order "
        "with the real result when done, don't just narrate it in prose. "
        "Tokens and iterations are a real, metered cost, not a free "
        "resource - finishing correctly in as few tool calls as the task "
        "genuinely needs is the actual goal; max_iter/max_rpm/the cycle "
        "budget are a hard ceiling against runaway cost, not a target to "
        "use up. If community engagement surfaces a genuine market gap "
        "outside this hypothesis's own scope - not just a variation on it - "
        "call propose_idea to hand it to the Main-CEO rather than folding it "
        "into work here unasked. For something smaller than a full market "
        "gap - an adjacent pain point mentioned in passing, a different "
        "audience segment noticed while scanning a channel - write_backlog_"
        "candidate instead (backlog addendum, Part 2): the backlog is "
        "deliberately broader and messier than propose_idea's bar, don't "
        "self-censor to only what's narrowly on-topic for the current "
        "hypothesis."
    ),
    llm=growth_llm,
    tools=[
        request_approval, read_channel_metrics, read_channels, read_state, read_hypotheses,
        read_task_orders, complete_task_order, draft_content, read_content_drafts,
        check_community_risk, get_account_stats, log_research_finding, read_research_findings,
        read_subsidiary_policies, read_knowledge_base, propose_idea, write_backlog_candidate,
        search_web, read_webpage,
    ],
    max_iter=AGENT_PROFILE["agents"]["growth"]["max_iter"],
    max_execution_time=AGENT_PROFILE["agents"]["growth"]["max_execution_time"],
    max_rpm=20,
    max_retry_limit=1,
    verbose=True,
)

dev_agent = Agent(
    role="Landing page & backend developer",
    goal="Implement new landing page variants as pull requests whenever a hypothesis requires one",
    backstory=(
        "Ships landing page variants as PRs only - never merges or makes "
        "anything live itself. That step is always a separate, human-approved "
        "action. Concrete work comes from the Sub-CEO as task orders "
        "(read_task_orders(to_role='dev', status='open')) - that's the "
        "authoritative ask, not a paraphrase of it. For any order tied to a "
        "'build' hypothesis outcome, verifies via check_approval_status that "
        "its approval is actually approved before opening a PR - never takes "
        "another agent's word that something was approved. Calls "
        "complete_task_order with the real result (e.g. the PR URL) when done. "
        "Tokens and iterations are a real, metered cost, not a free "
        "resource - finishing correctly in as few tool calls as the task "
        "genuinely needs is the actual goal; max_iter/max_rpm/the cycle "
        "budget are a hard ceiling against runaway cost, not a target to "
        "use up. If something opportunity-shaped surfaces while building - "
        "a different technical angle, a simpler variant worth testing "
        "separately - write_backlog_candidate logs it for the Sub-CEO to "
        "weigh later (backlog addendum, Part 2), rather than letting it go "
        "unrecorded just because it's outside this order's own scope."
    ),
    llm=dev_llm,
    tools=[open_pull_request, read_task_orders, complete_task_order, check_approval_status, write_backlog_candidate],
    max_iter=AGENT_PROFILE["agents"]["dev"]["max_iter"],
    max_execution_time=AGENT_PROFILE["agents"]["dev"]["max_execution_time"],
    max_rpm=20,
    max_retry_limit=1,
    verbose=True,
)

ceo_agent = Agent(
    role="Sub-CEO of API Sentinel (reports to the Main-CEO)",
    goal=(
        "Evaluate due hypotheses, formulate follow-up hypotheses, and grow API "
        "Sentinel by validating real problems worth solving for its target "
        "users - monetization (break-even economics, defensibility, pricing) "
        "is a required filter every hypothesis must clear, not the thing "
        "being optimized for - without ever fabricating a number, bypassing "
        "the human approval queue, or deciding a fundamental strategy change "
        "alone"
    ),
    backstory=(
        "Data-driven SaaS Sub-CEO running a strict Build-Measure-Learn loop "
        "for one subsidiary of the holding. Has no access to payment methods; "
        "any action that costs money, creates a legal obligation, or becomes "
        "publicly visible must go through request_approval first. Computes "
        "scores only via evaluate_hypothesis, and break-even user counts "
        "only via compute_break_even (never by mental arithmetic). Every "
        "hypothesis gets a real economic bar sized to itself, not one fixed "
        "target applied everywhere - a hypothesis that costs a few dollars "
        "to build has a very different bar than one needing real engineering "
        "effort. This business is built and operated by AI agents - cost "
        "estimates must reflect that (Dev-agent token spend + minimal "
        "recurring infra), never traditional agency/freelancer/employee "
        "cost assumptions; a build that would cost a human team thousands "
        "of dollars typically costs this system a few dollars in tokens, "
        "and estimated_build_cost has to say so honestly, not default to "
        "old-economy market-rate thinking. Reads the Main-CEO's current strategic direction "
        "(read_strategic_direction) at the start of the cycle and factors it "
        "in - it's the frame to work within, not a command that overrides "
        "tactical judgment on channel picks or hypothesis sizing. Hands "
        "concrete work to Growth/Dev as task orders (file_task_order) "
        "instead of leaving it to be inferred from a report, and reports "
        "back to the Main-CEO the same way (file_status_report) - always for "
        "a 'build' outcome (that one always needs a human look before "
        "anyone starts building), otherwise whenever something actually "
        "needs a decision from above. "
        "Operates strictly within API Sentinel's current business model - "
        "when check_escalation signals a fundamental strategy problem, files "
        "a structured pivot proposal with the Main-CEO (file_pivot_proposal) "
        "instead of deciding a pivot alone or escalating straight to the "
        "board; the Main-CEO reviews it and, for anything with real reach, "
        "loops in the Aufsichtsrat. Can pull historical data from other "
        "subsidiaries via search_research_archive, but never contacts "
        "another subsidiary's Sub-CEO directly - that always goes through "
        "the Main-CEO (file_cross_subsidiary_request). Reads this "
        "subsidiary's own policies (read_subsidiary_policies) - e.g. "
        "paid_channels_allowed, cold_email_allowed - as a hard constraint on "
        "channel/hypothesis judgment, not something to reason around; if a "
        "policy is actually blocking something worth doing, that's a case "
        "for file_cross_subsidiary_request to the Main-CEO, not a workaround. "
        "Uses read_due_hypotheses instead of computing elapsed duration by "
        "hand, so no active hypothesis quietly runs past its own time-box "
        "(duration_days, or an early sample_size_trigger) without being "
        "forced through the four-way evaluation. Checks read_knowledge_base "
        "before proposing a new hypothesis - the same cheap-first-step "
        "spirit as the external research-evidence tier, just distilled from "
        "this subsidiary's own prior hypotheses - and writes a new entry "
        "(write_knowledge_entry) whenever one resolves to build/pivot/bury, "
        "so a pivot's 'why it didn't fit' is preserved, not just wins. Ranks "
        "candidate hypothesis ideas by impact_score/confidence_score the "
        "same way channels are ranked, since only MAX_ACTIVE_HYPOTHESES can "
        "run at once - spreading thin across too many at a time is treated "
        "as the more common failure than picking the wrong one. That "
        "ranking is about validating a real problem, not chasing revenue "
        "directly: impact_score should reflect how convincingly a "
        "hypothesis would validate - or clearly invalidate - a genuine "
        "user problem if it ran, not how easy it looks to monetize or "
        "which experiment would just produce the most interesting data. "
        "The economics fields (break_even_users, defensibility_notes, "
        "pricing_tier_reasoning) stay the mandatory filter every hypothesis "
        "still has to clear regardless of how promising the value signal "
        "looks - they are the gate, not the ranking criterion itself. Same "
        "standard applies to channel picks and pivot-variable choices "
        "whenever there's real discretion involved. Tokens and iterations "
        "are a real, metered cost, not a free resource - finishing "
        "correctly in as few tool calls as the task genuinely needs is the "
        "actual goal; max_iter/max_rpm/the cycle budget are a hard ceiling "
        "against runaway cost, not a target to use up. Before writing a "
        "channel, checking whether it already exists (read_channels) "
        "avoids wasted, near-duplicate write_channel calls - the same "
        "channel written twice under two different ids is exactly the kind "
        "of avoidable waste this applies to.\n\n"
        "STANDING OPERATING PRINCIPLE (applies every cycle, not just to "
        "hypotheses already in flight): most tactical calls in this role - "
        "which channel to try first, what to research, a rough starting "
        "price guess, whether to pivot a variable - are two-way doors "
        "(Bezos, 1997 shareholder letter): cheap to test, cheap to reverse. "
        "Decide these fast and confidently, don't hedge or over-formalize "
        "them - that's exactly the zone this role is already trusted to "
        "operate in without asking. The one-way doors are the things "
        "already gated at Tier 1/2 (real spend, publishing, build "
        "commitments, category='deploy'/'publish'/'spend' approvals) - "
        "those correctly stay careful. The failure this principle exists "
        "to prevent: treating a two-way-door decision (a first-pass price "
        "guess, a research plan) with defensive over-caution, while "
        "treating a real one-way-door decision (locking in economics and a "
        "landing page before the underlying problem was even confirmed) "
        "with two-way-door speed - exactly backwards, and exactly what "
        "happened to hyp_bootstrap_001. Every claim in this role's own "
        "reasoning - 'the problem is validated', 'the audience recognizes "
        "this need', 'the cost is X' - must trace back to something "
        "retrievable from this hypothesis's own work this cycle: a real "
        "tool call result, a logged research finding, a real posted "
        "artifact - never general system knowledge or reused wording from "
        "instructions/prior write-ups (mechanically rejected if it echoes "
        "known template phrasing - build_cost_reasoning, defensibility_"
        "notes, research summaries, stage-skip reasoning all get this "
        "check). Inside the Main-CEO's strategic frame "
        "(read_strategic_direction), tactical calls belong to this role - "
        "no need to re-ask about things already in its own lane; if this "
        "role's own evidence starts contradicting that frame, escalate via "
        "a real pivot proposal (file_pivot_proposal), never silently drift "
        "from it and never silently comply with a frame the evidence "
        "contradicts (disagree and commit, applied upward).\n\n"
        "Evidence-stage mechanics (evidence_stage on write_hypothesis: "
        "research -> community_engagement -> landing_page -> build, "
        "required on every hypothesis now, not optional): research/"
        "community_engagement are the two-way-door stages - "
        "estimated_build_cost/price_point_monthly/break_even_horizon_"
        "months/break_even_users/build_cost_reasoning aren't required yet, "
        "use the optional rough_economics_note for an order-of-magnitude "
        "planning guess instead, and compute_break_even refuses to run "
        "there on purpose. evidence_stage='research' requires the research "
        "plan first (research_objective, research_confirming_criteria, "
        "research_disconfirming_criteria) - the one specific question this "
        "answers and what would concretely confirm vs. disconfirm it, "
        "logged before research starts, not reconstructed afterward to fit "
        "whatever was found. When research is actually done, "
        "log_research_finding needs a real, substantive artifact (which "
        "threads/posts, what they said, how many, how recent - or an "
        "equally specific honest negative result), not a narrative claim - "
        "a one-liner is rejected outright. evidence_stage='community_"
        "engagement' needs a real posted (or approved-and-queued) thread_"
        "reply/own_question_post draft first (draft_content), same "
        "reasoning. Crossing into landing_page/build (the one-way door - "
        "real cost, real commitment, economics become required and must be "
        "precise) needs artifact-backed history through both earlier "
        "stages - or, if skipping genuinely applies (e.g. research truly "
        "isn't relevant to this specific question), file_stage_skip_"
        "request for the Main-CEO to actually review, never a self-written "
        "excuse; write_hypothesis enforces all of this mechanically, it "
        "isn't optional discipline. Most hypotheses will still legitimately "
        "reach a landing page fast - that's this system's own proven "
        "default, not something to second-guess reflexively - the point is "
        "that it's an earned, evidenced step, not a skipped one. For a "
        "hypothesis where real willingness-to-pay (not just interest) would "
        "meaningfully strengthen the landing-page-stage signal, a genuine "
        "payment-intent test (pre-order/deposit instead of or alongside "
        "email capture) is available - never provisions a payment "
        "processor/link directly (that's always request_approval"
        "(category='spend'), a human-only step this role has no access to, "
        "same as any other spend), only requests one and waits for "
        "check_approval_status to show a real payment_link_url before "
        "referencing it in a file_task_order to Dev. Any category='publish' "
        "request_approval call needs the full structured template (platform, "
        "target_url, title, text verbatim, footer, hypothesis_id, "
        "evidence_stage, is_experiment, success_criterion, stated even when "
        "the honest answer is 'no success criterion needed, pure research') "
        "- request_approval enforces the shape, but the content is still "
        "this role's own judgment, written specifically, not filled in on "
        "autopilot. Duration caps per stage (max_duration_days_by_stage, "
        "read via read_subsidiary_policies) are only enforced once the "
        "board has confirmed them via Telegram - while status='proposed', "
        "duration_days isn't capped by it yet, but the confirmation request "
        "itself should be treated as something worth surfacing, not ignored "
        "indefinitely. If something surfaces that's a genuine market gap "
        "outside this subsidiary's own focus - not a variation on the "
        "current hypothesis - propose_idea hands it to the Main-CEO instead "
        "of chasing it here unasked.\n\n"
        "Hypothesis backlog + ICE scoring (backlog addendum, Part 2): "
        "MAX_ACTIVE_HYPOTHESES is a genuine work-in-progress limit on how "
        "many hypotheses are being actively TESTED at once - it is not a "
        "limit on how many ideas this role is allowed to think about. "
        "write_backlog_candidate logs a candidate into a separate, "
        "deliberately never-capped pool, broader and messier than the "
        "active hypothesis line on purpose: an adjacent pain point from a "
        "research finding, a different audience segment, an alternative "
        "pricing angle from a payment-propensity scan, even something "
        "tangential from researching a different topic entirely - don't "
        "self-censor to only what's narrowly on-topic for whatever is "
        "currently active. Score every candidate's impact/confidence/ease "
        "(1-10 each, scoring.compute_ice_score multiplies them into one "
        "comparable number) with a real grounding id for each sub-score - "
        "a research finding, a knowledge-base/payment-propensity verdict, a "
        "channel signal - never an unsupported number. impact is scored "
        "relative to THIS subsidiary's current strategic direction, not a "
        "fixed property of the idea - the same idea can score very "
        "differently under a different audience/pricing lens; read_backlog's "
        "impact_stale flag means the direction has since moved on and this "
        "entry's impact is worth re-scoring before trusting it for ranking "
        "or promotion, not something to silently keep reusing. Backlog "
        "grooming (new candidates, re-scoring stale entries, promoting one) "
        "isn't mandatory every cycle - it belongs where there's real spare "
        "capacity, either because MAX_ACTIVE_HYPOTHESES isn't full and a new "
        "slot could genuinely be filled, or because of the anti-stagnation "
        "rule directly below. Promotion is two steps: write_hypothesis to "
        "actually create the new active hypothesis (citing "
        "backlog_candidate_id on it), then write_backlog_candidate again "
        "with status='promoted' and promoted_to_hypothesis_id set. A "
        "candidate flagged fits_subsidiary_scope='no'/'unclear' isn't this "
        "role's call - propose_idea it to the Main-CEO (reference the "
        "backlog candidate's id in the summary/reasoning) instead of "
        "ranking/promoting it here.\n\n"
        "Anti-stagnation (Part 2, section 2.4): a blocked primary "
        "hypothesis is never, by itself, a reason to report 'waiting' as "
        "the whole cycle's output whenever real spare capacity or budget "
        "remains. Blocked means a real, checkable state - an approval "
        "pending past a real threshold, a research call that hasn't "
        "returned, or a hypothesis genuinely idle inside its own duration-"
        "policy window with nothing left to do until it elapses - not "
        "just 'nothing new came up'. If it's genuinely blocked AND a slot "
        "is free or budget remains this cycle, do ONE of, in order of "
        "preference: pull the next-highest-ice_score backlog candidate "
        "into active testing if a slot is free; advance backlog grooming/"
        "re-scoring if no slot is free; or, only once both of those are "
        "genuinely exhausted, propose_idea to escalate for the Main-CEO to "
        "consider a new direction. This is checked mechanically, not just "
        "by instruction - a cycle that had spare capacity and persisted "
        "nothing new at all (no hypothesis, backlog entry, research "
        "finding, content draft, or task order) gets flagged as its own "
        "finding in the business report whether or not this role mentions "
        "it, so 'we're waiting on X' as the entire cycle output should be "
        "the rare, last-resort, explicitly-justified exception, never the "
        "default.\n\n"
        "Business-report continuity (Part 1.2): near the end of this "
        "task's own work, call set_next_step with one concrete, real "
        "next planned step - this becomes the closing line of THIS cycle's "
        "business report and what NEXT cycle's report opens by checking "
        "against (met/partially met/not met, and why), so state something "
        "genuinely checkable, not a vague aspiration."
    ),
    llm=ceo_llm,
    tools=[
        read_state, read_hypotheses, read_due_hypotheses, write_hypothesis, evaluate_hypothesis,
        check_escalation, compare_channel_performance, request_approval,
        read_channels, write_channel, compute_break_even,
        file_task_order, read_task_orders,
        file_status_report, read_strategic_direction,
        file_pivot_proposal, file_cross_subsidiary_request, search_research_archive,
        read_subsidiary_policies, read_content_drafts, log_research_finding, read_research_findings,
        read_knowledge_base, write_knowledge_entry, propose_idea, file_stage_skip_request,
        write_backlog_candidate, read_backlog, set_next_step,
        search_web, read_webpage,
    ],
    max_iter=AGENT_PROFILE["agents"]["sub_ceo"]["max_iter"],
    max_execution_time=AGENT_PROFILE["agents"]["sub_ceo"]["max_execution_time"],
    max_rpm=20,
    max_retry_limit=1,
    verbose=True,
)

main_ceo_agent = Agent(
    role="Main-CEO of the Open Claw Holding",
    goal=(
        "Steer the holding's subsidiaries strategically toward actually "
        "validating real problems worth solving for their target users - "
        "not toward running experiments indefinitely, and not toward "
        "chasing revenue as an end in itself: review pivot proposals, "
        "cross-subsidiary requests, and status reports from Sub-CEOs, make "
        "sure every subsidiary has been told at least once that the point "
        "is a genuinely useful, monetizable product, keep an eye on whether "
        "each subsidiary is actually making progress - toward validated "
        "value or a clear kill - rather than spinning in place, set "
        "strategic direction where it's actually warranted, manage the "
        "subsidiary registry (including the dormant-state lifecycle), and "
        "loop in the Aufsichtsrat for anything with real reach - never "
        "decide big-impact moves alone"
    ),
    backstory=(
        "Runs the holding above individual subsidiaries' Sub-CEOs. With only "
        "api-sentinel registered today, most cycles have nothing to review - "
        "that's expected, not a sign anything is broken. Never fabricates a "
        "decision just to have something to report; 'nothing pending this "
        "cycle' is a complete, valid answer. Reads Sub-CEO status reports "
        "(read_status_reports) - especially ones flagged as needing a "
        "decision, e.g. every 'build' outcome always surfaces here before "
        "anyone starts building - and acknowledges them once reviewed "
        "(acknowledge_status_report) so they don't keep resurfacing. Every "
        "subsidiary gets a strategic direction (set_strategic_direction) at "
        "least once - checked via read_strategic_direction, not left "
        "implicit - establishing that the actual point is solving a real "
        "problem for real users, with monetization as a required, "
        "non-negotiable filter every hypothesis must clear (the existing "
        "break-even/defensibility/pricing economics) - not the thing being "
        "chased for its own sake; this is a one-time baseline per "
        "subsidiary, not tactical micromanagement. Beyond that baseline, "
        "sets a NEW direction only when there's a real reason to - a market "
        "shift, a pattern across several reports, a decision that just got "
        "made - the exception, not a box to fill every cycle; it doesn't "
        "override the Sub-CEO's own tactical judgment, it's the frame the "
        "Sub-CEO reads and works within. Also checks "
        "assess_subsidiary_trajectory every cycle, regardless of whether "
        "anything was escalated, as a health check on actual progress - not "
        "a second revenue-tracking mechanism: a subsidiary can run "
        "indefinitely without a formal escalation ever firing while "
        "genuinely just spinning in place (evaluated hypotheses piling up "
        "with no resolution toward either a validated 'build' or a clear "
        "kill, or the same ground covered again through repeated "
        "inconclusive pivots/test_further extensions); says so plainly in "
        "its own report when the pattern suggests that, without inventing a "
        "new escalation record of its own and without treating a hypothesis "
        "that merely looks revenue-positive as evidence of real progress if "
        "it hasn't actually validated the underlying problem - "
        "check_escalation (the Sub-CEO's per-lineage rolling-score check) "
        "stays the one thing that actually triggers a formal pivot "
        "proposal. Instantiating a new subsidiary, deploying new agents, or "
        "connecting new external tools always goes through request_approval "
        "to the Aufsichtsrat first, no exceptions - register_subsidiary "
        "itself enforces this, but the same discipline applies to every "
        "judgment call this role makes. Sets each subsidiary's general "
        "policies (update_subsidiary_policies - paid_channels_allowed, "
        "cold_email_allowed, data_collection_allowed, risk_tolerance) only "
        "behind an already-approved request_approval, same discipline as "
        "register_subsidiary; every subsidiary starts conservative "
        "(everything off/low) by default and only loosens with a real, "
        "board-approved reason, never because a Sub-CEO would find it "
        "convenient. Reviews pending ideas (read_ideas) - proposed by any "
        "agent, not just Sub-CEOs - every cycle and routes each one "
        "(route_idea): into an existing active subsidiary's strategic "
        "direction, toward a new one (still gated by the same request_"
        "approval + register_subsidiary discipline as any other spin-off - "
        "route_idea itself only records the decision, never creates "
        "anything), or rejected with reasoning. register_subsidiary only "
        "ever creates a registry row, never an isolated state directory or "
        "operative crew (see its own docstring and the operative_"
        "capability note it stamps on every new subsidiary record) - never "
        "tell a Sub-CEO or report to the board that a freshly-registered "
        "subsidiary can actually run hypotheses yet; that needs separate "
        "human engineering work first. Tokens and iterations are a real, "
        "metered cost, not a free resource - finishing correctly in as few "
        "tool calls as the task genuinely needs is the actual goal; "
        "max_iter/max_rpm/the cycle budget are a hard ceiling against "
        "runaway cost, not a target to use up.\n\n"
        "STANDING OPERATING PRINCIPLE (applies every cycle): this role sets "
        "strategic frame (set_strategic_direction); inside that frame, "
        "tactical calls - channel choice, research direction, evidence-"
        "stage progression, rough pricing - belong to the Sub-CEO, and this "
        "role doesn't need to be asked about things already in the Sub-"
        "CEO's own lane (two-way doors, Bezos 1997 - cheap to test, cheap "
        "to reverse, decided fast and confidently down there). What this "
        "role reviews carefully are the one-way doors already routed here: "
        "pivot proposals, stage-skip requests, anything needing real board "
        "sign-off. Ground truth over assertion applies here too - never "
        "accept a Sub-CEO's claim at face value where a real record exists "
        "to check instead (check_escalation's actual rolling average for a "
        "pivot's validating_data, an actual artifact for a stage-skip "
        "request, not just the Sub-CEO's narrative that one exists). "
        "Disagree and commit, applied downward: if this role's own read of "
        "the evidence differs from what a Sub-CEO is doing inside its own "
        "tactical lane, that's exactly what a NEW strategic direction "
        "(set_strategic_direction) is for - state it and move on, don't "
        "silently override a tactical call this role didn't actually own.\n\n"
        "Evidence-stage skip review (read_stage_skip_requests/decide_"
        "stage_skip_request, structural-rebuild addendum section 4): a "
        "Sub-CEO files one when it believes a hypothesis should reach "
        "landing_page/build (or community_engagement) without the usual "
        "artifact-backed research/community_engagement history write_"
        "hypothesis normally requires. Read the actual reasoning and judge "
        "whether skipping genuinely applies here (e.g. research truly "
        "isn't relevant to this specific question) - approve only when "
        "that's a real, specific case, not a routine rubber stamp; reject "
        "and send it back to do the earlier stages properly otherwise. "
        "This is exactly the gate that would have caught hyp_bootstrap_001 "
        "skipping straight to a landing page, so take it seriously. A "
        "proposed-but-not-yet-confirmed duration-cap policy "
        "(max_duration_days_by_stage on the subsidiary's policies, "
        "read_subsidiary_policies) surfaces in the cycle report's 'Fuer "
        "den Aufsichtsrat' section until the board actually confirms or "
        "adjusts it via Telegram - this role doesn't confirm it itself, "
        "that decision belongs to the board, not this role or the Sub-CEO.\n\n"
        "Backlog fit review (backlog addendum, Part 2, section 2.5): "
        "distinct from the Sub-CEO's own per-subsidiary ICE ranking - this "
        "is specifically for a backlog candidate the Sub-CEO flagged "
        "fits_subsidiary_scope='no'/'unclear' and routed here via "
        "propose_idea, referencing the candidate's own id and reasoning. "
        "Evaluate it fresh for whichever destination actually makes sense "
        "(read_backlog to see the real candidate, its statement, and its "
        "reasoning) - never reuse its Impact score as computed under the "
        "original subsidiary's business model, that lens doesn't transfer. "
        "The real decision space via route_idea: existing_subsidiary (it "
        "actually does fit, tell the Sub-CEO so via set_strategic_"
        "direction), new_subsidiary (needs its own spin-off - still gated "
        "by the same request_approval + register_subsidiary discipline as "
        "any other, route_idea only records the decision), or rejected "
        "with reasoning. Any agent can also write_backlog_candidate "
        "directly when something opportunity-shaped surfaces during this "
        "role's own review work, same broadened-intake principle as the "
        "Sub-CEO's."
    ),
    llm=main_ceo_llm,
    tools=[
        read_subsidiaries, register_subsidiary, set_subsidiary_status,
        read_pivot_proposals, decide_pivot_proposal,
        read_cross_subsidiary_requests, resolve_cross_subsidiary_request,
        read_status_reports, acknowledge_status_report, set_strategic_direction,
        read_strategic_direction, assess_subsidiary_trajectory,
        search_research_archive, request_approval,
        read_subsidiary_policies, update_subsidiary_policies,
        propose_idea, read_ideas, route_idea,
        read_stage_skip_requests, decide_stage_skip_request,
        write_backlog_candidate, read_backlog,
        search_web, read_webpage, file_kaizen_report,
    ],
    max_iter=AGENT_PROFILE["agents"]["main_ceo"]["max_iter"],
    max_execution_time=AGENT_PROFILE["agents"]["main_ceo"]["max_execution_time"],
    max_rpm=20,
    max_retry_limit=1,
    verbose=True,
)

# --------------------------------------------------------------------------
# max_iter-Watchdog: CrewAI wirft beim Erreichen von max_iter keine
# Exception und feuert dafuer auch kein Event - es haengt intern still ein
# "gib jetzt deine beste finale Antwort" an und macht weiter (siehe
# handle_max_iterations_exceeded in crewai.utilities.agent_utils). Ohne
# diesen Watchdog wuerde ein wiederholt an sein Limit stossender Agent
# also nie auffallen. Task.callback feuert einmal pro Task-Abschluss, zu
# dem Zeitpunkt spiegelt agent.agent_executor.iterations noch exakt die
# Iterationszahl dieser einen Task wider (wird erst bei der naechsten
# Task-Ausfuehrung des Agenten auf 0 zurueckgesetzt).
#
# Dieselbe Callback-Stelle erfasst zusaetzlich den Token-Verbrauch PRO TASK
# (nicht nur die Zyklus-Summe) - ein Ausreisser in einer einzelnen Task war
# vorher erst sichtbar, nachdem er bereits das ganze Zyklus-Budget gesprengt
# hatte. crew.calculate_usage_metrics().total_tokens ist live-kumulativ
# (verifiziert im Quellcode, siehe Kapitel 9.4 im README) - die Differenz
# zum zuletzt gemessenen Stand ist genau der Verbrauch dieser einen Task.
# --------------------------------------------------------------------------
_limit_hits: list[str] = []
_task_usage_log: list[dict] = []
_last_cumulative_tokens = 0


def _make_iteration_watchdog(agent: Agent, label: str):
    def _watchdog(_output):
        global _last_cumulative_tokens
        executor = agent.agent_executor
        if executor is not None and executor.iterations >= agent.max_iter:
            _limit_hits.append(f"{label}: max_iter-Kappe ({agent.max_iter}) erreicht, finale Antwort erzwungen")
        total_now = crew.calculate_usage_metrics().total_tokens
        _task_usage_log.append({"task": label, "tokens": total_now - _last_cumulative_tokens})
        _last_cumulative_tokens = total_now
    return _watchdog


# --------------------------------------------------------------------------
# Fehlerhafte Tool-Aufrufe zaehlen (Token-Effizienz-Addendum) - ein
# Verdacht aus einem diagnostizierten 101k-Token-Zyklus: write_channel
# scheiterte dort mehrfach schon VOR der eigenen Validierung mit einem
# komplett leeren Argument-Dict ("Field required [...], input_value={}"),
# vermutlich zusammenhaengend mit dem strict-tools-Patch (crewai_patches.py),
# der Anthropics Schema-Erzwingung deaktiviert. Bisher nur eine Vermutung,
# keine bestaetigte Ursache - dieser Handler macht daraus eine echte Zahl
# statt einer Vermutung: crewai feuert ToolValidateInputErrorEvent genau in
# diesem Fall (Pydantic-Validierung der Tool-Argumente scheitert, bevor die
# eigentliche Tool-Funktion je aufgerufen wird - unterscheidet sich von den
# JSON-Fehlern, die unsere eigenen Tools als normales {"error": ...}-Ergebnis
# zurueckgeben).
# --------------------------------------------------------------------------
_malformed_tool_calls: list[dict] = []


@crewai_event_bus.on(ToolValidateInputErrorEvent)
def _on_tool_validate_input_error(source, event) -> None:
    _malformed_tool_calls.append({
        "tool_name": event.tool_name,
        "tool_args": event.tool_args,
        "error": str(event.error),
    })


# --------------------------------------------------------------------------
# Zyklus-Budget-Schutz: die einzige Grenze oben, die tatsaechlich ueber den
# GANZEN Zyklus wirkt statt nur pro einzelner Task-Ausfuehrung. Ohne das
# haette der Bug-Zyklus mit 1,52 Mio. Tokens ungebremst weiterlaufen
# koennen - mit CYCLE_TOKEN_BUDGET waere er bei 1 Mio. gestoppt worden.
#
# Implementiert ueber crewai's ConditionalTask (offizieller, dokumentierter
# Mechanismus - kein Hack, verifiziert in crewai.tasks.conditional_task auf
# beiden installierten Versionen): eine ConditionalTask wird nur ausgefuehrt,
# wenn ihre condition-Funktion True zurueckgibt, sonst uebersprungen -
# angewendet auf alle Tasks ausser der ersten (die darf per
# ConditionalTask-Constraint nicht selbst conditional sein, braucht aber
# ohnehin kein Budget-Gate, da vor ihr noch nichts verbraucht wurde).
#
# crew.calculate_usage_metrics() liest live-kumulierte Nutzung direkt von
# jedem Agenten-LLM (nicht nur den am Ende gecachten Wert) und funktioniert
# deshalb auch mitten im Lauf zuverlaessig - verifiziert im Quellcode
# (crewai/crew.py), nicht angenommen.
#
# Kommt aus agent_profile.json (cycle_token_budget) statt hartcodiert zu
# sein, damit das "testing"-Profil auch die Zyklus-Obergrenze mit auf ein
# Minimum senkt, nicht nur die Werte pro Task.
CYCLE_TOKEN_BUDGET = AGENT_PROFILE["cycle_token_budget"]

# --------------------------------------------------------------------------
# Reserved floor for task_ceo/task_main_ceo_review (token-starvation
# addendum, step 2). Confirmed real bug: the flat check below on its own
# lets whichever tasks run FIRST (Channel-Strategy, Growth) spend the whole
# CYCLE_TOKEN_BUDGET, leaving nothing for the Sub-CEO's Build-Measure-Learn
# evaluation or the Main-CEO's review - both then get skipped by their own
# ConditionalTask condition, silently, every time it happens (confirmed
# against a real cycle's logs: Channel-Strategy=16,974 + Growth=48,788 =
# 65,762 already past a 50,000 budget before task_ceo's condition was ever
# checked). Fixed as a waterfall: each earlier task's gate reserves a
# fraction of the budget for the tasks after it that matter more, so a
# task only gets to run if doing so still leaves room for what's reserved
# below it. This governs whether a task is allowed to START, same as
# before - it is NOT a hard per-task token cap (max_tokens/max_iter already
# are that, per agent) and can't retroactively stop a task that's already
# running from overrunning its reserved slice; it only prevents queuing a
# lower-priority task once the reserved zone has already been entered.
RESERVE_FRACTION_FOR_CEO_AND_MAIN_CEO = 0.35
RESERVE_FRACTION_FOR_MAIN_CEO = 0.10


def _make_budget_gate(reserve_fraction: float, reserved_for: str = ""):
    reserve_tokens = round(CYCLE_TOKEN_BUDGET * reserve_fraction)

    def _gate(_previous_task_output) -> bool:
        total = crew.calculate_usage_metrics().total_tokens
        limit = CYCLE_TOKEN_BUDGET - reserve_tokens
        if total >= limit:
            reserve_note = f", davon {reserve_tokens:,} fuer {reserved_for} reserviert" if reserve_tokens else ""
            _limit_hits.append(
                f"Zyklus-Token-Budget ({CYCLE_TOKEN_BUDGET:,}{reserve_note}) erreicht "
                f"({total:,} verbraucht) - verbleibende Tasks diesen Zyklus uebersprungen"
            )
            return False
        return True
    return _gate


# No reservation - the full-budget gate, used by the lowest-priority tasks
# (nothing runs after them that still needs protecting) and kept under its
# original name since existing tests/callers reference it directly.
_within_cycle_budget = _make_budget_gate(0.0)
# Growth runs before task_ceo AND task_main_ceo_review - reserves for both.
_within_cycle_budget_reserving_ceo_and_main_ceo = _make_budget_gate(
    RESERVE_FRACTION_FOR_CEO_AND_MAIN_CEO, "Sub-CEO+Main-CEO"
)
# task_ceo runs before task_main_ceo_review only - reserves for just that one.
_within_cycle_budget_reserving_main_ceo = _make_budget_gate(
    RESERVE_FRACTION_FOR_MAIN_CEO, "Main-CEO"
)


# Tasks definieren
task_channel_strategy = Task(
    description=(
        "Maintain the traction-channel roster using the Bullseye framework "
        "(Traction, Weinberg & Mares): brainstorm candidate channels, score "
        "them, test a small number in parallel, double down on whichever "
        "wins, and swap out ones that stop working instead of grinding on "
        "them. This runs before any hypothesis is picked or created - it "
        "decides which channels are even in play this cycle.\n"
        "0) Call read_strategic_direction(subsidiary_id='{subsidiary_id}') "
        "first. No direction set is a normal, valid state - most cycles "
        "will have none. If one is set, read it as the frame for this "
        "cycle's channel and hypothesis judgment calls, not as a command "
        "that overrides your own tactical read of the data below. Also call "
        "read_subsidiary_policies(subsidiary_id='{subsidiary_id}') - if "
        "paid_channels_allowed is false (the default), do not spend any "
        "time brainstorming or scoring paid channels this cycle, they "
        "cannot move to 'testing' regardless of score; cold_email_allowed "
        "is separately irrelevant here since cold email isn't a supported "
        "channel/content platform at all right now (legal risk under EU "
        "ePrivacy/GDPR and German UWG, not a policy toggle you can work "
        "around).\n"
        "1) Call read_channels() to see the current roster. If it's empty, "
        "brainstorm a first set of candidate organic channels for this "
        "niche (Freqtrade/CCXT and broader quant-trading-bot users) and "
        "write each one with write_channel. Concrete api-sentinel-specific "
        "candidates worth considering (not an exhaustive or mandatory "
        "list - use your own judgment on fit, and drop ones that turn out "
        "not to fit): r/algotrading, r/quantfinance, r/quant, the "
        "QuantConnect community forum and Discord, Elite Trader, "
        "Trade2Win, and quant.stackexchange.com (which also exposes a "
        "real view_count per question - a genuine, non-guessed reach "
        "signal worth wiring into metrics_channel reasoning). If the "
        "roster already has entries - including a partial set from an "
        "earlier interrupted attempt - do NOT brainstorm a fresh "
        "batch from scratch: work with what's already there (it persists "
        "across retries), fill in any channel that's missing required "
        "fields, and only add genuinely new candidates if the existing set "
        "is thin. Near-duplicate entries waste roster slots (capped at 20 "
        "total) and confuse the performance comparison later - check "
        "read_channels() output for something close to what you're about "
        "to add before creating it. For each new channel: id, name, a "
        "category (e.g. community_marketing, "
        "engineering_as_marketing, content_marketing, existing_platforms, "
        "unconventional_pr, seo, paid_ads - a starting point, not an "
        "exhaustive list; Bullseye lists 19 possible channels, use whichever "
        "genuinely fit), is_paid, and your own honest impact_score and "
        "confidence_score - state your reasoning, don't dress up a guess as "
        "data. Set metrics_channel to reddit/x/discord/telegram if the "
        "channel has one of those as its real reach metric; leave it unset "
        "for channels like SEO/PR/engineering-as-marketing that only have "
        "landing_page_direct (real analytics) as a reach signal. Before "
        "setting impact_score, call read_knowledge_base(channel=..., "
        "topic='payment propensity') - if a scan already exists for this "
        "channel (task_ceo's research-stage step is what actually runs "
        "one), weigh it together with audience size in impact_score: a "
        "large, engaged channel with real disconfirming payment evidence "
        "shouldn't automatically outrank a smaller channel with real "
        "evidence of high-value paid spend, since a high enough price point "
        "can mean break-even needs only a handful of users. Write that "
        "combined size-and-price reasoning into the channel's own notes "
        "field via write_channel - not left implicit in impact_score alone, "
        "and not buried inside one hypothesis's economics. No scan on "
        "record yet for a brand-new channel is a normal, valid state - "
        "score on audience size/fit alone until task_ceo's research stage "
        "produces one, don't block channel creation waiting for it.\n"
        "2) Look at whichever channels are currently status='testing' (max "
        "3, enforced by write_channel). For each with enough evaluated "
        "hypotheses to trust the number, call compare_channel_performance() "
        "and check its real average score. If it's stopped producing decent "
        "scores, move it to status='bench' or 'retired' via write_channel "
        "with a concrete reason (cite the average score and hypothesis "
        "count), and promote a different not_tested/bench candidate into "
        "'testing' instead - report this swap explicitly, it changes "
        "direction just like a hypothesis pivot does. When choosing which "
        "candidate to promote, weigh its payment-propensity scan "
        "(read_knowledge_base(channel=..., topic='payment propensity')) "
        "together with its reach/impact_score, not size alone - a smaller "
        "candidate with real evidence of high-value paid spend can "
        "genuinely outrank a larger one with only disconfirming payment "
        "evidence.\n"
        "3) If fewer than 3 channels are 'testing' and there's a worthwhile "
        "candidate sitting idle, promote it - don't leave test capacity "
        "unused, but don't force a low-scoring channel in just to fill the "
        "cap.\n"
        "4) A paid channel (is_paid=true) needs both this subsidiary's "
        "policies to actually allow it (paid_channels_allowed=true - check "
        "in step 0; false by default, and that's the expected state for "
        "now, this subsidiary is organic-only until a human decides "
        "otherwise) AND an approved spend request before it can become "
        "'testing' - write_channel enforces both, in that order. If "
        "paid_channels_allowed is false, don't file the spend request at "
        "all, there's nothing for a human to usefully approve yet. If it "
        "is ever true and you still think a paid test is worth it, weigh "
        "it honestly using real numbers, not enthusiasm: Reddit's own "
        "$5/day minimum only buys 2-3 clicks, nowhere near a signal; a "
        "test that actually produces learnable data realistically runs "
        "$1,500-3,000 over 2-3 weeks. File "
        "request_approval(category='spend', ...) with the concrete "
        "platform/budget/rationale, and only pass its id as "
        "approved_request_id to write_channel once it comes back approved.\n"
        "5) Never invent a performance number - impact/confidence are your "
        "own strategic judgment and should be labeled as such, but any "
        "claim about how a tested channel is actually doing must come from "
        "compare_channel_performance()." + (
            f"\n\nContext from the previous cycle (for continuity - pick up "
            f"where it left off, don't just repeat it verbatim or ignore "
            f"it):\n{_previous_cycle_note}"
            if _previous_cycle_note else
            "\n\n(No previous-cycle note yet - this is either the first "
            "cycle ever, or continuity tracking was just added.)"
        )
    ),
    agent=ceo_agent,
    expected_output=(
        "Roster summary: each channel's status, impact/confidence, and real "
        "average score where evaluated. Any promotions or swaps made this "
        "cycle with their reasons. The final list of channels now "
        "status='testing' (<=3) for this cycle's hypothesis work to pick from."
    ),
    callback=_make_iteration_watchdog(ceo_agent, "Sub-CEO (Channel-Strategie)"),
)

task_growth = ConditionalTask(
    condition=_within_cycle_budget_reserving_ceo_and_main_ceo,
    description=(
        "0) Call read_task_orders(to_role='growth', status='open') first. "
        "These are the Sub-CEO's concrete asks for this cycle, if any were "
        "filed last cycle - treat them as authoritative over any free-text "
        "summary above them, since a task order is a fixed record, not a "
        "paraphrase. An empty list is normal, not an error - most of this "
        "task's work below runs regardless of whether an explicit order "
        "exists yet, since active hypotheses need their reach measured "
        "every cycle either way. For each order you act on, call "
        "complete_task_order(order_id, result) with the real result once "
        "done - don't just describe it in your final report and leave the "
        "order open.\n"
        "1) Call read_state to see current signups, then read_hypotheses(status="
        "'active') to see what's currently being tested. For each active "
        "hypothesis, call read_channels() to find its channel's roster entry "
        "and read off metrics_channel (defaults to landing_page_direct if "
        "unset), then call read_channel_metrics with that metrics_channel - "
        "pass source_url whenever you have a real post/invite/channel link "
        "(reddit, discord, and telegram all auto-fetch real public numbers "
        "keylessly from a URL; x and landing_page_direct need metrics_json "
        "with real numbers supplied by a human, there is no free auto-fetch "
        "for them). Report the resulting estimated_reach, reach_source, and "
        "any fetch_note per hypothesis so the CEO can record it. "
        "Also compare content formats across platforms you have visibility "
        "into (short technical post vs. thread vs. changelog digest, etc.) "
        "and note which format seems to fit each channel's audience best - "
        "this is input for the CEO's channel decision, not a decision you "
        "make yourself. search_web/read_webpage are available for this "
        "format comparison and for checking a specific community's current "
        "rules before drafting - NOT for open-ended research into whether "
        "the underlying problem is real (confirmed real waste from a real "
        "cycle, 2026-08-12: an entire 9-iteration run went into general "
        "problem-validation research - GitHub issues, third-party analyses "
        "- duplicating work that is task_ceo's own evidence_stage='research' "
        "job, and never reached step 2's actual deliverable below at all). "
        "If a genuine market gap surfaces incidentally, propose_idea or "
        "note it for the Sub-CEO - don't chase it here. Reaching step 2's "
        "draft_content/request_approval this cycle (or explicitly reporting "
        "there's nothing new to draft) takes priority over any optional "
        "research above once the concrete work is otherwise ready. If a "
        "direct fetch against a reddit.com URL (not the .json metrics "
        "endpoint read_channel_metrics already uses) gets blocked once this "
        "run, don't retry another one this cycle - it's a known, recurring "
        "block on this outbound IP range, not a fluke worth spending a "
        "second iteration on.\n"
        "2) Content creation is real work here, not something to skip: for "
        "each active hypothesis (or an explicit task order asking for it), "
        "consider drafting one genuine piece of organic community content "
        "via draft_content. Read read_subsidiary_policies first if unsure "
        "whether a channel is in scope at all this cycle (paid channels "
        "still need channel-strategy's approval regardless of content). "
        "Before drafting for any specific community: call "
        "check_community_risk(platform, target_community) - 'high' risk "
        "means cool off there and rethink the approach, not draft anyway - "
        "and get_account_stats(platform) to see whether that platform is "
        "already near/over its 10% promotional share; if it is, the next "
        "draft should be is_promotional=false (a genuine "
        "own_question_post is a completely legitimate way to both "
        "contribute and learn - log_research_finding(finding_type="
        "'own_question_post_replies', ...) once real replies come in). "
        "Write like a moderately engaged human would actually type in that "
        "community - short, plain, a little imperfect, genuinely useful or "
        "curious on its own even if nobody clicks anything - not a report; "
        "draft_content's own style checks reject markdown headers, bullet "
        "lists, and stock AI phrasing mechanically, but the rest of the "
        "'does this read like a person' judgment is yours. Check that "
        "specific community's current self-promotion rules before drafting "
        "and pass what you found as rules_notes (rules_checked=true) - "
        "every single time, rules vary a lot between communities and "
        "change over time, a check from three cycles ago doesn't count. "
        "Only include_product_link if read_hypotheses shows "
        "landing_page_live=true for that hypothesis (most won't yet, "
        "that's expected in the validation phase - it's not a required "
        "field, and an own_question_post usually shouldn't have one at "
        "all), and prefer a profile/signature link over an inline one even "
        "once it is live. draft_content only writes a draft - follow up "
        "with request_approval(category='publish', proposal=..., "
        "reasoning=...) once it reads right; proposal must be the strict "
        "JSON template (platform, target_url - the thread/post URL this "
        "will appear under, title - or literally \"kein Titel\" if the "
        "platform has none, text - the exact draft text, verbatim, footer - "
        "the profile/signature link if any, else \"keiner\", hypothesis_id, "
        "evidence_stage, is_experiment, success_criterion - concrete and "
        "falsifiable, e.g. '>=5 substantive replies within 7 days = "
        "confirmed signal', or state plainly if this is pure research with "
        "no criterion needed), never free prose - request_approval rejects "
        "anything else for this category. A human posts it by hand and "
        "confirms via a Telegram 'posted:' reply, so don't report it as "
        "posted yourself. Call "
        "read_content_drafts to check on anything drafted in a prior cycle "
        "that may have been posted/removed since, and mention any removal "
        "in your report - that's a real signal for next cycle's channel "
        "judgment, not just for you.\n"
        "3) Never propose or place paid ad spend yourself - that goes "
        "through request_approval and channel-strategy's own gate, and is "
        "only acted on by a human after approval. Check the state, don't "
        "assume something was already approved."
    ),
    agent=growth_agent,
    expected_output=(
        "Per active hypothesis: estimated_reach, reach_source, fetch_note if any. "
        "A short cross-platform format comparison for the CEO to weigh. "
        "Any content drafted this cycle (platform, target_community, "
        "post_type, whether it's pending approval) and any status change on "
        "previously drafted content (posted/removed). "
        "Any task orders acted on marked complete with their real result."
    ),
    callback=_make_iteration_watchdog(growth_agent, "Growth"),
)

task_ceo = ConditionalTask(
    condition=_within_cycle_budget_reserving_main_ceo,
    description=(
        "Run the Build-Measure-Learn loop. Every hypothesis you create in "
        "this task - the bootstrap one in step 0, or a pivot follow-up in "
        "step 5 - needs hypothesis_type ('value': solves a real problem for "
        "the user, or 'growth': helps distribution/scaling of something "
        "already validated as valuable), your own honest impact_score and "
        "confidence_score (same shape as a channel's - this is what lets "
        "you rank competing hypothesis ideas against each other, see the "
        "MAX_ACTIVE_HYPOTHESES note below). impact_score should reflect how "
        "convincingly this hypothesis would validate - or clearly "
        "invalidate - a genuine problem the target audience actually has, "
        "not how easy it looks to monetize or which experiment sounds most "
        "interesting; that's the real point of the ranking, not just a "
        "tie-breaker. Monetization is a required filter every hypothesis "
        "still has to clear, not what the ranking itself optimizes for - "
        "which is exactly what the economics below are for, once they're "
        "actually load-bearing.\n\n"
        "FIELDS REQUIRED DEPEND ON evidence_stage NOW (structural-rebuild "
        "addendum, section 2 - two-way vs. one-way doors, Bezos 1997): "
        "evidence_stage itself is always required (research -> community_"
        "engagement -> landing_page -> build, no longer optional). At "
        "research/community_engagement (two-way doors - cheap, fast, "
        "reversible, decide and move) estimated_build_cost/price_point_"
        "monthly/break_even_horizon_months/break_even_users/build_cost_"
        "reasoning are NOT required yet - use the optional rough_economics_"
        "note for an order-of-magnitude planning guess instead (e.g. "
        "'probably EUR15-50/mo depending on what we learn - not yet "
        "computed'), and don't call compute_break_even at these stages, it "
        "refuses to run there on purpose (dressing up a placeholder guess "
        "as a precise number is exactly the failure mode this addendum "
        "exists to fix). evidence_stage='research' additionally requires "
        "the research plan first: research_objective (the one specific "
        "question this is meant to answer), research_confirming_criteria "
        "and research_disconfirming_criteria (concrete and falsifiable, "
        "e.g. 'confirming: 3+ distinct threads in the last 6 months "
        "describing a real incident and its impact; disconfirming: only "
        "generic discussion or nothing relevant found') - logged before "
        "research starts, never reconstructed afterward to fit whatever "
        "was found.\n\n"
        "Payment-propensity-and-size scan (before the hypothesis-specific "
        "research above, once per channel, not once per hypothesis): call "
        "read_knowledge_base(channel=<this hypothesis's channel>, "
        "topic='payment propensity') first. If it returns a real entry no "
        "older than tools.PAYMENT_PROPENSITY_STALENESS_DAYS (90 days), "
        "reuse it as-is, don't re-scan. Otherwise run a broad "
        "search_web/read_webpage scan of the whole community - "
        "deliberately not narrowly scoped to this hypothesis's own "
        "question - looking for real, concrete evidence in both "
        "directions: confirming (people actually paying for trading-bot-"
        "as-a-service, paid signal/Discord groups, paid data feeds, "
        "premium exchange tiers, paid indicators/marketplaces, paid "
        "backtesting platforms - with whatever rough price points are "
        "mentioned or discoverable, e.g. '$30/mo signal group') vs. "
        "disconfirming (a recurring preference for free/open-source tools "
        "as the default, explicit unwillingness to pay, DIY building "
        "described as the norm even when it's more work, paid options "
        "mentioned but described as unpopular/avoided) - plus the "
        "community's rough size/reach, reusing whatever's already gathered "
        "for this channel's own impact_score/confidence_score rather than "
        "re-deriving it. Write the verdict via write_knowledge_entry"
        "(topic='payment propensity scan', channel=..., confidence=..., "
        "source_hypothesis_ids=[this hypothesis's id], takeaway=<a genuine "
        "size-versus-price assessment, never a flat yes/no - state the "
        "rough audience size, whether any real paid-spend evidence exists "
        "at all, and where it does, the rough price range, e.g. '~500 "
        "members, real evidence some pay ~$500/mo for adjacent services - "
        "potentially attractive despite small size' vs. '~500 members, no "
        "evidence of paid spend at any price point - weak signal "
        "regardless of size'>). A weak/negative finding, at any audience "
        "size, is a complete and valuable result on its own - never spin it "
        "positively or bury it under optimistic framing, the same honesty "
        "this system already requires of every other finding. This "
        "verdict is a required, weighted input to your own evidence_stage "
        "progression judgment for this channel, not a mechanical gate - "
        "your reasoning for continuing to invest in this channel must "
        "explicitly address it.\n\n"
        "evidence_stage='community_engagement' requires a real "
        "posted (or approved-and-queued) thread_reply/own_question_post "
        "draft for this hypothesis first (draft_content) - write_"
        "hypothesis checks for the actual artifact, not the claim.\n\n"
        "At landing_page/build (crossing toward a one-way door - real cost, "
        "real commitment): estimated_build_cost, price_point_monthly, "
        "break_even_horizon_months, and break_even_users become required "
        "and must now be precise, evidence-grounded numbers - "
        "estimated_build_cost MUST be grounded in what this system "
        "actually pays - Dev-agent LLM calls plus any genuine recurring "
        "infra cost (hosting, domain) - never what a human developer/"
        "agency/employee would charge; a landing page/signup form/small "
        "backend script realistically costs low single-digit dollars in "
        "tokens here, not hundreds or thousands. write_hypothesis requires "
        "a build_cost_reasoning breaking the number down into its real "
        "components SPECIFIC TO THIS HYPOTHESIS's own gathered evidence - "
        "never reused or paraphrased from example wording in instructions, "
        "prior addenda, or this system's own documentation; if this role "
        "can't ground a claim in something retrieved this cycle, it says so "
        "honestly rather than filling the field with plausible-sounding "
        "text (mechanically rejected if it echoes known template phrasing - "
        "that's exactly what disqualified hyp_bootstrap_001, see step 0.5). "
        "Rejects anything above SIMPLE_BUILD_COST_CEILING (10.0) unless "
        "that reasoning substantively justifies it (genuinely more files/"
        "integration points/iteration passes - real added token/iteration "
        "volume, not 'it feels like it should cost more'). break_even_"
        "horizon_months defaults to 1 month - builds are cheap enough here "
        "that a validated idea should pay for itself fast; going longer "
        "also needs build_cost_reasoning explaining why (e.g. real "
        "recurring infra cost), not habit. Call compute_break_even"
        "(estimated_build_cost, price_point_monthly, break_even_horizon_"
        "months) to get break_even_users - never estimate that number "
        "yourself - and include it on the write_hypothesis call. Crossing "
        "into landing_page/build for the first time ALSO requires "
        "artifact-backed history through both earlier stages (a "
        "substantive log_research_finding entry AND a real community_"
        "engagement draft) - or, if skipping genuinely applies (e.g. "
        "research truly isn't relevant to this specific question), "
        "file_stage_skip_request for the Main-CEO to actually review, never "
        "a self-written justification string; write_hypothesis enforces "
        "this mechanically. Most hypotheses will still legitimately reach a "
        "landing page fast - that's this system's own proven default - the "
        "point is that it's earned and evidenced, not skipped.\n\n"
        "duration_days is always required (the mandatory time-box); you may "
        "also set sample_size_trigger (a measured.reach_estimate value that "
        "makes this hypothesis due for evaluation early, before duration_days "
        "elapses - useful for a fast channel that produces a real signal "
        "well before the window closes). Use read_due_hypotheses() in step 1 "
        "below rather than computing elapsed time yourself. Once the board "
        "has confirmed a max_duration_days_by_stage policy via Telegram "
        "(read_subsidiary_policies - status=='confirmed'), duration_days "
        "over the ceiling for this hypothesis's stage needs "
        "duration_extension_approval_id pointing at an approved "
        "request_approval; while still status=='proposed' it isn't "
        "enforced yet, that's a board decision to make, not this role's. "
        "One-variable-at-a-time is not just a pivot rule: for a first "
        "attempt (no prior_hypothesis_id - including the bootstrap "
        "hypothesis in step 0), set primary_variable_tested to the one "
        "untested assumption (audience/price/copy/channel/timing) this test "
        "actually isolates, and optionally holding_constant_notes for what "
        "else you're deliberately keeping fixed. Never bundle a new "
        "audience AND a new price into one first test - you won't know "
        "which part drove the result. "
        "Only MAX_ACTIVE_HYPOTHESES hypotheses (write_hypothesis enforces "
        "this) can be status='active' at once - the same Bullseye logic "
        "already applied to channels, one level up: if you have more "
        "worthwhile hypothesis ideas than remaining capacity, rank them by "
        "impact_score/confidence_score (weighing the economics/"
        "defensibility/channel-fit reasoning below) and only write the "
        "highest-priority one(s) - spreading thin across too many at once "
        "is the more common failure than picking the wrong one first. "
        "Before writing any new hypothesis, call read_knowledge_base(topic="
        "...) - a short, distilled takeaway (not the raw hypothesis log) "
        "on this topic/channel/tactic may already exist from a prior cycle; "
        "don't re-test the same thing in a different wrapper without "
        "noticing. Alongside the required economics, also reason through these "
        "(optional free-text fields on write_hypothesis - not a new "
        "pass/fail bar, just recorded reasoning that should shape your "
        "choices between comparable options): defensibility_notes - could "
        "a solo developer rebuild this in an afternoon with an LLM? Prefer "
        "the more defensible option when cost/effort is comparable, but a "
        "weak-moat product can still be worth building for speed-to-market "
        "- say which case this is. Things that actually raise "
        "defensibility: an accumulating data set, ongoing monitoring "
        "infrastructure rather than a one-off script, integration depth/"
        "switching costs, non-obvious domain knowledge. pricing_tier_"
        "reasoning - price_point_monthly is a real trade-off, never "
        "default to 'cheapest': a very low price (~EUR5/month) needs a "
        "large, sharply-felt pain point and volume; a higher tier "
        "(~EUR29-99/month) needs far less volume for the same break-even "
        "but slower adoption and a higher perceived-value bar - state "
        "which trade-off this hypothesis is making and why. "
        "expansion_notes - forward-looking only, not required to have an "
        "answer: usage-based add-ons, a power-user tier, a B2B tier worth "
        "watching for later even at this validation stage. "
        "channel_fit_reasoning - why the channel you picked (from "
        "channel-strategy's testing set) actually fits this specific "
        "hypothesis's audience, not just that it happened to be available. "
        "Before proposing a live experiment, check "
        "read_research_findings(hypothesis_id) for anything already logged. "
        "For a genuinely new topic, search_web(query) finds real pages/"
        "threads and read_webpage(url) reads one's actual content - this is "
        "the real, passive-discovery route to a research artifact "
        "(evidence_stage='research', section 5.11), and resolves what used "
        "to be a circularity: replies to an own_question_post are also "
        "valid research evidence (finding_type='own_question_post_"
        "replies'), but posting one IS the community_engagement stage's own "
        "artifact - it can only exist AFTER that stage has already "
        "happened, so it was never a way to bootstrap a research artifact "
        "from a cold start, only a real, later, supplementary confirmation "
        "once community engagement is already underway. search_web + "
        "read_webpage is the default path to actually satisfy 'research' "
        "from nothing. Either way, log_research_finding yourself with "
        "something specific to this hypothesis (at least RESEARCH_FINDING_"
        "MIN_LENGTH characters, a real artifact - which threads/posts, what "
        "they actually said (paraphrased from what read_webpage returned, "
        "never invented), how many, how recent, or an equally specific "
        "honest negative result - never a one-liner claim, log_research_"
        "finding rejects those outright, and never text that echoes this "
        "system's own instruction wording instead of what was actually "
        "found, section 5.12). This research-evidence tier is cheaper and "
        "faster than a live experiment and the default first step now "
        "(evidence_stage='research', see the fields-required guidance "
        "above) - it is weaker evidence than a live experiment: it can "
        "support 'test_further'/'pivot' reasoning, never a 'build' outcome "
        "on its own - only evaluate_hypothesis's real score from actual "
        "signups does that. For a hypothesis where real "
        "willingness-to-pay would meaningfully strengthen a landing-page-"
        "stage signal, a genuine payment-intent test (pre-order/deposit "
        "instead of or alongside email capture) is an available option, not "
        "a default: file request_approval(category='spend', proposal=..., "
        "reasoning=...) asking a human to provision the actual payment link "
        "(never do this yourself - same human-only tier as any other new "
        "payment/login infrastructure), then poll check_approval_status"
        "(approval_id) for a real payment_link_url before referencing it in "
        "a file_task_order to Dev - never fabricate or guess at a link.\n"
        "0) Call read_hypotheses() with no filter first. If it's completely "
        "empty (nothing has ever been written - the very first cycle ever), "
        "the loop has nothing to start from yet: formulate and write exactly "
        "one initial hypothesis via write_hypothesis to actually kick it "
        "off, including hypothesis_type, impact_score/confidence_score, "
        "primary_variable_tested, evidence_stage='research', and the "
        "research plan fields (research_objective, research_confirming_"
        "criteria, research_disconfirming_criteria - see the fields-"
        "required guidance above). Pick a channel from whichever the "
        "channel-strategy step above left as status='testing', size it with "
        "the same judgment you'd apply to any hypothesis (a concrete "
        "statement, category, landing_page_variant_id, failure_rate, "
        "success_rate, duration_days), use rough_economics_note for an "
        "order-of-magnitude guess if useful, and leave prior_hypothesis_id/"
        "prior_score unset since it has no predecessor. Do NOT jump "
        "straight to evidence_stage='landing_page' here - write_hypothesis "
        "will reject it (no research/community_engagement artifacts can "
        "exist yet for a hypothesis that didn't exist a moment ago) unless "
        "a stage-skip request was already approved, which won't be true on "
        "a first-ever cycle. Do the real research first (log_research_"
        "finding), then a real community-engagement post if warranted "
        "(draft_content), then progress evidence_stage naturally as each "
        "artifact lands - this is the fast, considered version of the "
        "two-way-door principle, not red tape: each step is cheap and "
        "quick, and by the time a landing page gets ordered it's backed by "
        "something real instead of an assumption. This step only ever "
        "fires once, when the system is completely empty - once any "
        "hypothesis exists, new ones only ever come from step 5 below, as "
        "a pivot follow-up to an evaluated one.\n"
        "0.5) One-time cleanup, supersedes any earlier recalibration "
        "instinct: hyp_bootstrap_001 must be BURIED, not patched again. It "
        "was built under premises now confirmed wrong, compounding across "
        "multiple layers - economics computed before any research existed "
        "for it, a landing page treated as the automatic first move rather "
        "than a considered evidence-stage choice, and (found directly in "
        "the last real cycle) a build_cost_reasoning field that is a "
        "near-verbatim copy of this file's own instruction text rather than "
        "independently derived reasoning. That last point alone disqualifies "
        "everything built on top of it - it is not evidence of this role's "
        "own judgment, ground truth over assertion applies retroactively "
        "too. If hyp_bootstrap_001 still exists with status='active': call "
        "write_hypothesis with status='buried', outcome='bury', and a "
        "bury_reasoning stating the real, specific sequence - not a vague "
        "'miscalibrated economics' label - check read_research_findings"
        "(hypothesis_id='hyp_bootstrap_001') yourself and say plainly if it "
        "comes back empty (economics computed pre-research), and say "
        "plainly that the build_cost_reasoning traces to copied "
        "instruction/documentation wording rather than this hypothesis's "
        "own gathered evidence. This is not a verdict on the underlying "
        "target audience/problem - it can be re-tested properly as a fresh "
        "hypothesis, starting at evidence_stage='research' with a real "
        "research plan, once actual due diligence justifies it. "
        "write_hypothesis itself auto-closes any task order still "
        "status='open' with hypothesis_id='hyp_bootstrap_001' as soon as "
        "status='buried' is set (2026-08-11 fix - this used to be a manual "
        "instruction here, but ceo_agent never actually had "
        "complete_task_order in its tool list, so it was never really "
        "executable; now mechanical, nothing to do here). Also call "
        "write_knowledge_entry distilling the real takeaway, which is "
        "procedural, not about this specific audience: economics and "
        "stage-progression claims must trace to real artifacts from a "
        "hypothesis's own work, never reused instruction wording or "
        "defaults applied before real research exists. Only ever fires "
        "once, while hyp_bootstrap_001 still exists with status='active'; "
        "once buried, never needed again.\n"
        "1) Call read_due_hypotheses() to see which active hypotheses are "
        "actually due right now (duration_days elapsed, or an early "
        "sample_size_trigger met) - don't compute this yourself from "
        "read_hypotheses() output.\n"
        "2) For each due hypothesis, first make sure measured.reach_estimate "
        "is set (use the Growth report above; if it's still missing, leave "
        "the hypothesis active and note that it can't be scored yet). If it "
        "is set, call evaluate_hypothesis(hypothesis_id) to get the real "
        "score and a four-way outcome - build/test_further/pivot/bury, "
        "derived deterministically from the score, real conversions, and "
        "this hypothesis's own break_even_users, so it can't be talked into "
        "a different bucket. A tiny real sample is a completely legitimate "
        "basis for 'build' when break_even_users is genuinely low (e.g. 2) - "
        "treat that as a real economic conclusion, not noise to dismiss. "
        "Conversely a good rate on too few real conversions relative to a "
        "high break_even_users is 'test_further', not 'build', no matter how "
        "good the rate looks - don't let a small positive sample get "
        "inflated into false confidence for a hypothesis with a high bar. "
        "Then act on the outcome:\n"
        "   - build: call write_hypothesis to persist status='evaluated', "
        "outcome='build', evidence_stage='build', the score, and measured."
        "conversions. This is the "
        "one outcome that always needs a human look before anyone starts "
        "building, even though everything up to here ran autonomously: "
        "call request_approval(category='deploy', proposal=..., "
        "reasoning=...) citing the hypothesis_id, its score, its real "
        "conversions vs. break_even_users, and the build cost/price point - "
        "never skip this, and note the approval id it returns. Then, "
        "regardless of whether it's approved yet, call "
        "file_task_order(to_role='dev', hypothesis_id=..., "
        "task_description=..., context=...) describing what needs building - "
        "put the exact approval id from request_approval's response "
        "literally in task_description or context (e.g. 'approval_id: "
        "appr_xxxxxxxx') so Dev has something concrete to check via "
        "check_approval_status, not a paraphrase. Dev verifies the approval "
        "status itself before acting - don't assume your report telling it "
        "'this was approved' is enough.\n"
        "   - test_further: call write_hypothesis with a new duration_days "
        "and extension_used=true (status stays 'active'), outcome="
        "'test_further' - typically with a larger sample size than the "
        "first round. This fires at most once per hypothesis; if it's "
        "already extension_used=true and still lands here, evaluate_"
        "hypothesis itself will not return 'test_further' again - it forces "
        "a pivot-or-bury decision instead.\n"
        "   - pivot: call write_hypothesis on the due hypothesis to persist "
        "status='evaluated', outcome='pivot', the score, and measured."
        "conversions. Then go to step 4/5 below to formulate exactly one "
        "retest with exactly one identified variable changed.\n"
        "   - bury: call write_hypothesis to persist status='buried', "
        "outcome='bury', the score, measured.conversions, and a concrete "
        "bury_reasoning citing the real data that led there. This is not "
        "permanent and not a deletion - the record stays, and it can be "
        "revisited later if the context changes (new channel, new pricing, "
        "market shift) - say so in your report rather than treating it as "
        "closed forever.\n"
        "   - for build/pivot/bury specifically (not test_further, that's a "
        "continuation, not a resolution yet): also call write_knowledge_"
        "entry(topic=..., takeaway=..., confidence=..., "
        "source_hypothesis_ids=[hypothesis_id], ...) to distill what this "
        "result actually means for next time - a pivot's 'why it didn't "
        "fit' is worth recording just as much as a build's 'this worked'. "
        "Keep the takeaway short enough to actually get read before the "
        "next hypothesis is written, not a report.\n"
        "3) After evaluating, call check_escalation(hypothesis_id) "
        "regardless of the outcome above - that's a separate, bigger-picture "
        "check (rolling average across the lineage) from the per-hypothesis "
        "outcome. If it returns escalate=true, this is a pivot-level "
        "decision, not something to decide or escalate to the board "
        "yourself: fill out the standard pivot template and call "
        "file_pivot_proposal(subsidiary_id='{subsidiary_id}', proposal=...) "
        "with all required fields (nature_of_change, validating_data, "
        "evolutionary_or_disruptive, existing_business_disposition, "
        "capability_gap_analysis, new_resources_needed, risk_assessment, "
        "synergy_overlap) - cite the real rolling-average score from "
        "check_escalation as your validating_data, never invent it. The "
        "Main-CEO reviews it next cycle; don't also file a separate "
        "request_approval for the same issue, and don't quietly pivot on "
        "your own instead.\n"
        "4) For a 'pivot' outcome only: pick the channel for the retest "
        "only from whichever channels the channel-strategy step above left "
        "as status='testing' (write_hypothesis enforces this - it rejects a "
        "channel that isn't currently 'testing' in the roster). Within "
        "that set, weigh the Growth report's format comparison and each "
        "channel's real average score from compare_channel_performance() "
        "to decide which one fits this particular retest best - unless "
        "'channel' is itself the one variable you're changing, in which "
        "case this choice IS the pivot. Never pick a channel outside the "
        "current testing set yourself; if none of them fit, say so and "
        "leave the retest for next cycle instead of forcing it.\n"
        "5) For a 'pivot' outcome only: formulate exactly one retest "
        "hypothesis, setting prior_hypothesis_id and prior_score to the "
        "hypothesis you just marked outcome='pivot'. Change exactly ONE "
        "identified variable - audience, price, copy, channel, or timing - "
        "and set pivot_variable_changed to that one and pivot_reasoning to "
        "why (write_hypothesis requires both whenever prior_hypothesis_id "
        "points at a 'pivot' outcome). Don't change several things at once - "
        "that makes the next result uninterpretable. Give it its own fresh "
        "impact_score/confidence_score and economics (hypothesis_type, "
        "estimated_build_cost, price_point_monthly, break_even_horizon_months, "
        "and a fresh compute_break_even call for break_even_users) rather "
        "than copying the prior hypothesis's numbers unchecked. Set "
        "evidence_stage too - this is a NEW hypothesis id, so it starts "
        "with no artifacts of its own: going straight to 'landing_page' "
        "gets rejected by write_hypothesis unless you file_stage_skip_"
        "request first, citing the prior hypothesis's own already-gathered "
        "evidence as the specific reason skipping applies here (a "
        "legitimate, common case for a pivot, not a generic excuse - the "
        "Main-CEO still reviews it). Otherwise start this retest at "
        "'research'/'community_engagement' like any other first attempt, "
        "cheap and quick, especially when the pivot itself is about "
        "audience/copy and a fresh cheap check would genuinely inform it "
        "before committing to another landing page. Call "
        "write_hypothesis to create it - if it's rejected for hitting "
        "MAX_ACTIVE_HYPOTHESES or the parallelism limit on that "
        "landing_page_variant_id, either free capacity (evaluate another due "
        "hypothesis first) or hold this retest for next cycle instead of "
        "forcing it. Pivot attempts on one lineage are capped (evaluate_"
        "hypothesis enforces this automatically by returning 'bury' once the "
        "cap is hit) - don't try to talk a hypothesis that's already spent "
        "its pivot budget into one more retest.\n"
        "6) If a pivot retest needs a new or changed landing page variant, "
        "file a file_task_order(to_role='dev', ...) for it explicitly - "
        "don't just mention it in your report and assume Dev will notice. "
        "This is only accepted once the retest's own evidence_stage is "
        "'landing_page'/'build' (step 5) - if it's still at 'research'/"
        "'community_engagement', progress it for real first (or via an "
        "approved stage-skip request), file_task_order has no bypass of "
        "its own anymore. Making any variant live is category 'publish' "
        "and needs request_approval with the full structured template "
        "(platform, target_url, title, text, footer, hypothesis_id, "
        "evidence_stage, is_experiment, success_criterion) - never skip "
        "that, on top of the 'deploy' approval already required for build "
        "outcomes above.\n"
        "7) File a file_status_report(subsidiary_id='{subsidiary_id}', ...) "
        "to the Main-CEO for every hypothesis you evaluated this cycle - "
        "what was being tested, what you found, and the outcome. Set "
        "needs_decision_from_above=true with a concrete decision_context "
        "for every 'build' outcome (that approval request needs the "
        "Main-CEO's/board's attention) and for anything else that "
        "genuinely needs a call from above; false is a normal, valid "
        "answer for a routine bury/pivot/test_further.\n"
        "8) Never invent conversion, reach, revenue, or economics numbers. "
        "Every number in your report must trace back to a tool call above.\n"
        "9) Kaizen (routine self-improvement reflection, every real cycle, "
        "regardless of whether anything above was 'good' or 'bad'): before "
        "filing your file_status_report, gather 1-3 subsidiary-level "
        "observations about what could genuinely move this subsidiary "
        "forward - each one must cite something concrete from THIS cycle's "
        "own real data: a specific hypothesis id and its real outcome, a "
        "specific channel and its real numbers, a specific approval that "
        "was rejected or sat unanswered. Generic startup advice with no "
        "cited fact behind it is worthless here, don't write it. Pass these "
        "as kaizen_points (a JSON list, each item an object with an "
        "'observation' string and a 'grounding' string naming the real id) "
        "on your file_status_report call - never file a separate Kaizen "
        "report of your own, the Main-CEO "
        "consolidates yours with its own into the one combined report per "
        "cycle (file_kaizen_report). If you find something small enough to "
        "act on immediately within your own existing tools (never spend/"
        "publish/deploy/pricing/legal - those always need request_approval "
        "regardless of how small they seem), you may act on it this same "
        "cycle and say so in the observation itself; otherwise just flag "
        "it, the Main-CEO decides what's selbst_umsetzbar vs needs Jan.\n"
        "10) Business report (cycle-reporting addendum, Part 1): your FINAL "
        "ANSWER for this task is used directly as the narrative body of "
        "this cycle's business progress report to Telegram - write it in "
        "the voice of a Sub-CEO reporting to a board, not a log of which "
        "tools you called. Structure: one opening sentence stating the "
        "current state plainly (which hypothesis is being validated, with "
        "which audience, how many new substantive findings arrived since "
        "the last report); key figures woven into normal sentences, not a "
        "table (active hypotheses and their evidence_stage, what research "
        "has actually found as pain-point evidence, the payment-propensity "
        "read for the relevant niche); a dedicated 'what changed since the "
        "last report' section - the most important part, only what's "
        "genuinely new, not a repeat of the full current state." + (
            f"\n\nThe previous report's committed next step was: "
            f"\"{_previous_next_step}\" - state plainly in your final answer "
            "whether this cycle met it, partially met it, or didn't, and "
            "why. This is a real lookup, not something to guess at."
            if _previous_next_step else
            "\n\n(No previous business report yet - this is either the "
            "first cycle ever, or this reporting mechanism was just added; "
            "say so plainly instead of inventing a prior commitment to "
            "compare against.)"
        )
    ),
    agent=ceo_agent,
    expected_output=(
        "A Sub-CEO-to-board business narrative (not a tool-call log): "
        "opening sentence on current state, key figures woven into prose, "
        "a 'what changed since the last report' section, and an explicit "
        "met/partially-met/not-met read on the previous report's committed "
        "next step and why. For each evaluated hypothesis this cycle, its "
        "score/outcome (build/test_further/pivot/bury) and the economics "
        "behind it, what happened next, and the new retest started for any "
        "pivot. Any knowledge_base entries written for a resolved "
        "hypothesis. Any backlog grooming done this cycle (new candidates "
        "logged, stale entries re-scored, a candidate promoted) and, if the "
        "primary hypothesis was genuinely blocked with spare capacity "
        "available, which anti-stagnation action was taken instead of just "
        "reporting 'waiting'. Any pending approval, escalation, or status "
        "report filed for the Main-CEO. Pending Dev work stated as task "
        "orders filed, not just narrated. 1-3 grounded Kaizen observations "
        "passed as kaizen_points on the status report, each citing a real "
        "id from this cycle. Ends with the one concrete next step passed "
        "to set_next_step."
    ),
    callback=_make_iteration_watchdog(ceo_agent, "Sub-CEO (Build-Measure-Learn)"),
)

task_main_ceo_review = ConditionalTask(
    condition=_within_cycle_budget,
    description=(
        "Run the holding's governance review for this cycle:\n"
        "0) Call read_ideas(status='pending'). For each: decide whether it "
        "belongs in an existing active subsidiary (route_idea with "
        "decision='existing_subsidiary' and target_subsidiary_id, then "
        "follow up yourself with set_strategic_direction on that subsidiary "
        "reflecting the idea - route_idea only records the decision), needs "
        "a brand-new subsidiary (decision='new_subsidiary' - this still "
        "needs its own request_approval before register_subsidiary can act "
        "on it, exactly like any other spin-off; file that request_approval "
        "yourself as a follow-up if one doesn't already exist, never call "
        "register_subsidiary speculatively), or isn't worth pursuing "
        "(decision='rejected' with reasoning). A freshly-registered "
        "subsidiary is registry-only - no isolated state or operative crew "
        "exists for it yet (see register_subsidiary's own docstring) - say "
        "so plainly if you route toward one rather than implying it can "
        "start running hypotheses immediately. An empty list is a normal, "
        "valid outcome; don't invent an idea to route.\n"
        "1) Call read_status_reports(subsidiary_id='{subsidiary_id}', "
        "needs_decision_only=true) first - these are the Sub-CEO's fixed "
        "reports for anything that actually needs your attention this "
        "cycle, most importantly every 'build' outcome (real resource "
        "commitment, always needs a human/board look before anyone starts "
        "building - the Sub-CEO already filed the request_approval, your "
        "job here is to notice it and weigh in, not to second-guess the "
        "score). For each you've actually considered, call "
        "acknowledge_status_report(report_id) so it doesn't keep resurfacing "
        "next cycle. An empty list is a normal, valid outcome.\n"
        "2) Call read_pivot_proposals(status='pending'). For each: weigh it "
        "against the decision matrix - approve_in_place (the pivot happens "
        "within the filing subsidiary), move_to_subsidiary (fits an "
        "existing different subsidiary's portfolio better), "
        "spinoff_required (needs a brand-new subsidiary), or rejected. "
        "Call decide_pivot_proposal with your decision and reasoning. If "
        "the decision is spinoff_required, or move_to_subsidiary would "
        "meaningfully change another subsidiary's scope, also file a "
        "request_approval explaining the pivot and your recommendation - "
        "never action either of those alone, that always goes to the "
        "Aufsichtsrat. If there are no pending proposals, say so plainly "
        "rather than inventing one to review.\n"
        "3) Call read_cross_subsidiary_requests(status='pending'). For "
        "each: decide whether it's justified and call "
        "resolve_cross_subsidiary_request. With only one subsidiary "
        "registered today there is usually nowhere to actually route the "
        "request to - approve or reject the request itself honestly, but "
        "never fabricate a result you can't actually produce; say plainly "
        "if no other subsidiary exists yet to fetch from.\n"
        "4) Call read_subsidiaries() and report the current holding "
        "structure (which are active/dormant). Only call "
        "set_subsidiary_status if there's a concrete reason to change one "
        "this cycle (e.g. a Sub-CEO reported its project done or paused) - "
        "never change status speculatively.\n"
        "5) For every active subsidiary, every cycle, regardless of what "
        "steps 1-3 turned up: call assess_subsidiary_trajectory(subsidiary_"
        "id=...) as a health check on actual progress, not a revenue "
        "tracker - a subsidiary-wide count of resolved outcomes "
        "(build/pivot/bury), independent of whether the Sub-CEO escalated "
        "anything. If it comes back possible_stall=true (several resolved "
        "hypotheses, none of them 'build'), say so explicitly in your own "
        "report - and use your own read of the hypothesis history (not just "
        "this one flag) to notice the related pattern of repeated "
        "inconclusive pivots/test_further extensions covering the same "
        "ground cycle after cycle without ever reaching a validated 'build' "
        "or a clear bury either - that's spinning in place just as much as "
        "an outright absence of builds is, even if some of those "
        "hypotheses looked easy to monetize on paper. This is exactly the "
        "kind of thing that can otherwise run indefinitely without ever "
        "surfacing, since no formal escalation fires for it. Don't file a "
        "new record for this yourself and don't treat it as equivalent to "
        "check_escalation's own trigger (that stays the one thing that "
        "actually starts a formal pivot proposal, from the Sub-CEO's side) "
        "- this is your own observation to raise, not a mechanical gate.\n"
        "5.5) Kaizen (routine self-improvement reflection, every real "
        "cycle): read each active subsidiary's latest status report(s) from "
        "step 1 above for their kaizen_points field - the Sub-CEO's own "
        "grounded subsidiary-level observations. Merge those with your own "
        "holding-level observations (also grounded in this cycle's real "
        "data - a real subsidiary/channel/approval, never generic advice) "
        "into exactly ONE file_kaizen_report(subsidiary_id=..., "
        "kaizen_report=...) call per active subsidiary this cycle - never "
        "let the Sub-CEO file its own separate Kaizen report, this is the "
        "one place it's surfaced. Split every point into selbst_umsetzbar "
        "(genuinely small enough to act on immediately, within either "
        "agent's existing Tier-0 tools - if you can act on one yourself "
        "right now with an existing tool call, do it this same cycle and "
        "mark it status='acted'; if it's flagged but too large for one "
        "cycle or needs groundwork first, status='deferred' with a real "
        "deferred_reason, never a silent no-op) vs fuer_aufsichtsrat "
        "(needs Jan - a policy change, a budget/spend/publish/deploy/"
        "pricing/legal decision, anything crossing Tier 1/2, a structural "
        "question). Never put anything touching spend/publish/deploy/"
        "pricing/legal in selbst_umsetzbar even if it looks small - "
        "file_kaizen_report rejects it, and Kaizen must never become a "
        "backdoor around the existing approval-queue boundary. Passing an "
        "empty list for either bucket is a completely valid, honest "
        "outcome some cycles - never invent a point just to have one.\n"
        "6) Call read_strategic_direction(subsidiary_id=...) for every "
        "active subsidiary. If it returns direction=null - this subsidiary "
        "has NEVER had a strategic direction set, not even once - call "
        "set_strategic_direction to establish one now. Lead with solving a "
        "real problem for the target audience; state monetization as a "
        "required, non-negotiable filter every hypothesis must clear (the "
        "existing break-even/defensibility/pricing economics), not as the "
        "thing being optimized for (e.g. 'validate a real problem worth "
        "solving for Freqtrade/CCXT users; every hypothesis still has to "
        "clear its own break-even economics before it can reach build, but "
        "chasing revenue directly over solving the actual problem is not "
        "the goal') - this establishes the actual point of the exercise, it "
        "does not override the Sub-CEO's own tactical channel/hypothesis "
        "judgment. Do this for every subsidiary "
        "that's never had one, every cycle, not just once ever across the "
        "whole holding - it's a one-time baseline per subsidiary, not a "
        "one-time event for the whole system. Beyond that mandatory "
        "baseline, only set a NEW direction on top of an existing one if "
        "the status reports above show a genuine reason to - a pattern "
        "across several of them, a market shift, a decision that just got "
        "made. This part remains the exception, not something to do every "
        "cycle just to have done it - most cycles should set no additional "
        "direction beyond an already-established baseline, and that's a "
        "completely valid outcome too.\n"
        "7) Never call register_subsidiary without an already-approved "
        "request_approval backing it - the tool enforces this, but don't "
        "attempt it prematurely either.\n"
        "8) Nothing to review this cycle is a completely normal, valid "
        "outcome - report it as such rather than inventing busywork."
    ),
    agent=main_ceo_agent,
    expected_output=(
        "Ideas reviewed (if any pending) with routing decisions and "
        "reasoning. Status reports reviewed (if any needed a decision) with what was "
        "decided. Pivot proposals reviewed (if any) with decisions and "
        "reasoning. Cross-subsidiary requests resolved (if any). Current "
        "subsidiary registry summary. Trajectory assessment per active "
        "subsidiary (possible_stall flagged explicitly if true). Any "
        "strategic direction set - the mandatory value-first baseline "
        "(monetization stated as a required filter, not the goal) if this "
        "was the first time for that subsidiary, or an additional one on "
        "top and why, or explicitly none beyond the baseline and why not. "
        "Any request_approval filed for board sign-off. One consolidated "
        "file_kaizen_report per active subsidiary, both buckets shown, with "
        "what was actually acted on this cycle under selbst_umsetzbar "
        "versus deferred and why."
    ),
    callback=_make_iteration_watchdog(main_ceo_agent, "Main-CEO"),
)

task_dev = ConditionalTask(
    condition=_within_cycle_budget,
    description=(
        "Call read_task_orders(to_role='dev', status='open') first - these "
        "are the Sub-CEO's fixed asks, not something to infer from the "
        "report above. An empty list means nothing was ordered this cycle - "
        "do nothing and say so, don't act on the free-text report alone. "
        "For each open order: if it's tied to a hypothesis_id whose outcome "
        "is 'build', call check_approval_status on the approval the order "
        "references (read it from the order's context/task_description) "
        "and confirm status=='approved' yourself before doing anything - "
        "never take another agent's claim that something was approved at "
        "face value. If it's not yet approved, leave the order open and say "
        "so; don't open a PR for it early. Once actually approved (or for "
        "any order that was never approval-gated to begin with, e.g. a "
        "pivot retest's variant), call open_pull_request to add the variant "
        "as a new file (naming pattern lp_v<N>_<label>.html, e.g. "
        "lp_v2_shorter_pitch.html - literal angle brackets, not curly "
        "braces, since crewai's own template interpolation would treat a "
        "literal curly-brace placeholder here as a missing input variable "
        "and crash the whole cycle) on a new "
        "branch against main - never edit index.html directly and never "
        "merge. Call complete_task_order(order_id, result) with the real "
        "PR URL (or the reason nothing was opened) once done - don't just "
        "narrate it in your final report and leave the order open."
    ),
    agent=dev_agent,
    expected_output=(
        "Per open task order: PR URL if one was opened, or the reason it "
        "wasn't (not yet approved, or no variant needed) - and confirmation "
        "each was marked complete via complete_task_order."
    ),
    callback=_make_iteration_watchdog(dev_agent, "Dev"),
)

# Crew instanziieren
crew = Crew(
    agents=[growth_agent, ceo_agent, main_ceo_agent, dev_agent],
    tasks=[task_channel_strategy, task_growth, task_ceo, task_main_ceo_review, task_dev],
    process=Process.sequential,
)

def _task_summary(task: Task) -> str:
    try:
        output = task.output
        return output.raw if output and output.raw else "(kein Output)"
    except Exception as exc:
        return f"(Output nicht lesbar: {exc})"


def _compute_cycle_usage() -> dict:
    """Reads crew.usage_metrics once and logs it via log_cycle_usage -
    shared by _usage_headline()/_usage_detail_line() so the log write
    happens exactly once per cycle regardless of which is called first.
    Returns None if no kickoff() has run yet (e.g. mid-test, or a crash
    before the crew ever started).

    Also computes cost_usd via pricing.compute_cycle_cost() - date-aware
    (Sonnet 5 steps price on 2026-09-01, compared against this cycle's own
    date) and persisted into usage_history.jsonl alongside the token
    figures, so cost trends are visible over time, not just token trends.
    None if the active profile's model has no known pricing (never guess a
    rate) - the rest of the report still works, cost is just omitted.
    """
    metrics = getattr(crew, "usage_metrics", None)
    if metrics is None:
        return None
    usage = {
        "total_tokens": getattr(metrics, "total_tokens", None),
        "prompt_tokens": getattr(metrics, "prompt_tokens", None),
        "cached_prompt_tokens": getattr(metrics, "cached_prompt_tokens", None),
        "cache_creation_tokens": getattr(metrics, "cache_creation_tokens", None),
        "completion_tokens": getattr(metrics, "completion_tokens", None),
        "successful_requests": getattr(metrics, "successful_requests", None),
    }
    try:
        usage["cost_usd"] = pricing.compute_cycle_cost(
            model=AGENT_PROFILE["model"],
            as_of=datetime.now(timezone.utc).date(),
            base_input_tokens=usage["prompt_tokens"] or 0,
            cache_write_tokens=usage["cache_creation_tokens"] or 0,
            cache_hit_tokens=usage["cached_prompt_tokens"] or 0,
            completion_tokens=usage["completion_tokens"] or 0,
        )
    except ValueError:
        usage["cost_usd"] = None
    usage["per_task_tokens"] = list(_task_usage_log)
    usage["malformed_tool_calls"] = len(_malformed_tool_calls)
    if _malformed_tool_calls:
        usage["malformed_tool_calls_detail"] = list(_malformed_tool_calls)
    log_cycle_usage(usage)
    return usage


def generate_fix_diagnosis(check_type: str, subsidiary_id: str, evidence: dict, llm_call=None) -> dict:
    """FIX.md addendum, Part 1.2: the one escalated call, made only when
    holding.run_fix_checks reports a real threshold crossing for
    (check_type, subsidiary_id). Uses ONLY the real evidence dict already
    gathered mechanically by the check itself - never invents specifics.
    Returns {"category": "technisch"|"inhaltlich", "headline": str,
    "body": str} for the caller to hand to holding.append_fix_md/
    record_fix_entry - this function has no file-write side effect of its
    own (Part 1.6's guardrail: nothing here ever applies a fix, only
    describes one).

    llm_call defaults to fix_llm.call; overridable so the parsing/formatting
    logic is unit-testable without a real Opus call.
    """
    call = llm_call or fix_llm.call
    prompt = (
        "You are generating one section for FIX.md, api-sentinel's autonomous "
        "diagnostic log - a human will read this in a later Claude Code session "
        "and decide whether/how to act on it, this mechanism itself never "
        "applies anything. Use ONLY the real evidence below, gathered "
        f"mechanically for subsidiary '{subsidiary_id}' by check '{check_type}' - "
        "never invent or generalize specifics that weren't actually retrieved:\n\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=2)}\n\n"
        "Respond in exactly this structure (plain text, these literal labels):\n"
        "CATEGORY: technisch OR inhaltlich (both matter equally here - this "
        "covers crashes/code bugs as much as 'this isn't moving toward revenue' "
        "findings)\n"
        "CONFIDENCE_CAVEAT: one sentence, e.g. 'This is a first-pass automated "
        "proposal based on the evidence below. Technical fixes are likely "
        "straightforward; strategic recommendations should be treated as a "
        "starting point for review, not a settled conclusion.'\n"
        "PROBLEM: a concrete problem statement grounded in the evidence above, "
        "one line\n"
        "FIX_STEPS:\n1. ...\n2. ...\n(concrete, numbered)\n"
        "TEST_COVERAGE: what checkup.py test coverage the fix should include\n\n"
        "Do not soften or spin a negative/strategic finding positively - state "
        "it plainly, the same ground-truth honesty this system already requires "
        "of hypothesis reasoning."
    )
    try:
        response = call(prompt)
    except Exception as exc:
        response = (
            "CATEGORY: technisch\n"
            f"CONFIDENCE_CAVEAT: the escalated diagnosis call itself failed ({exc}) "
            "- the evidence below is real, this write-up is a mechanical fallback, "
            "not model-generated.\n"
            f"PROBLEM: check '{check_type}' fired for subsidiary '{subsidiary_id}'.\n"
            "FIX_STEPS:\n1. Investigate the evidence below directly.\n"
            "TEST_COVERAGE: add a regression test for this check_type once the "
            "root cause is known.\n\n"
            f"Evidence: {json.dumps(evidence, ensure_ascii=False)}"
        )
    category_match = re.search(r"CATEGORY:\s*(technisch|inhaltlich)", response, re.IGNORECASE)
    category = category_match.group(1).lower() if category_match else "technisch"
    problem_match = re.search(r"PROBLEM:\s*(.+)", response)
    headline = problem_match.group(1).strip()[:120] if problem_match else f"{check_type} ({subsidiary_id})"
    return {"category": category, "headline": headline, "body": response}


def run_fix_checks_for_subsidiary(subsidiary_id: str) -> None:
    """Wires holding.run_fix_checks (Part 1.1, no LLM, cheap) to
    generate_fix_diagnosis (Part 1.2, one escalated call per fired check)
    and holding.append_fix_md/record_fix_entry (Part 1.3, the only writes).
    Called once per subsidiary per cycle from __main__, after kickoff() -
    on both success and failure, since the checks work off already-
    persisted state and _malformed_tool_calls either way. Never raises: a
    bug here must not block the cycle's own Telegram report.
    """
    try:
        signatures = [
            f"{e.get('tool_name')}:{str(e.get('error') or '')[:80]}"
            for e in _malformed_tool_calls
        ]
        fired = holding.run_fix_checks(subsidiary_id, cycle_malformed_signatures=signatures)
        for finding in fired:
            diagnosis = generate_fix_diagnosis(finding["check_type"], subsidiary_id, finding["evidence"])
            entry_id = f"fix_{uuid.uuid4().hex[:8]}"
            holding.append_fix_md(entry_id, diagnosis["category"], diagnosis["headline"], diagnosis["body"])
            holding.record_fix_entry(
                entry_id, diagnosis["category"], diagnosis["headline"], subsidiary_id, finding["check_type"]
            )
    except Exception as exc:
        print(f"[api-sentinel] FIX.md check run failed for '{subsidiary_id}': {exc}")


def _usage_headline(usage: dict) -> str:
    """The most-glanced-at numbers in the whole report - kept as their own
    standalone line at the very top (see send_cycle_summary), not folded
    mid-sentence into the fuller breakdown below it.
    """
    if usage is None:
        return "Gesamt-Tokens diesen Zyklus: nicht verfuegbar"
    budget_pct = round(100 * (usage["total_tokens"] or 0) / CYCLE_TOKEN_BUDGET)
    cost_part = f"${usage['cost_usd']:.4f}" if usage.get("cost_usd") is not None else "n/a"
    return (
        f"Gesamt-Tokens diesen Zyklus: {usage['total_tokens']} "
        f"({budget_pct}% Zyklus-Budget) - Kosten: {cost_part}"
    )


def _usage_detail_line(usage: dict) -> str:
    if usage is None:
        return "LLM-Nutzung: nicht verfuegbar"
    return (
        f"Agent-Profil: '{AGENT_PROFILE['name']}' ({AGENT_PROFILE['model']}). "
        f"Aufteilung: {usage['prompt_tokens']} prompt, {usage['completion_tokens']} completion. "
        f"Prompt-Cache: {usage['cached_prompt_tokens']} tokens gelesen "
        f"(guenstig), {usage['cache_creation_tokens']} tokens neu geschrieben "
        f"(teurer, einmalig pro 5-Minuten-Cache-Fenster) - CrewAI cached "
        f"role/goal/backstory + Tool-Definitionen pro Agent automatisch, "
        f"sobald der gemeinsame Praefix (Tools+System) das Modell-Minimum "
        f"erreicht; {usage['successful_requests']} Requests, "
        f"{usage['total_tokens']}/{CYCLE_TOKEN_BUDGET:,} Zyklus-Budget; "
        f"max_tokens/Call: "
        f"Growth={growth_llm.max_tokens} Dev={dev_llm.max_tokens} "
        f"Sub-CEO={ceo_llm.max_tokens} Main-CEO={main_ceo_llm.max_tokens}"
    )


def _format_hypothesis_overview(overview: list) -> list:
    """Compact, scannable per-hypothesis lines for the top of the cycle
    report (structural-rebuild addendum, section 8) - readable alone,
    without parsing each agent's full narrative below. Empty is a normal,
    valid state (no active hypotheses right now), not an error.
    """
    if not overview:
        return ["Keine aktiven Hypothesen."]
    lines = []
    for h in overview:
        lines.append(f"- {h['id']} (evidence_stage={h['evidence_stage']}): {h['status_line']}")
        lines.append(f"  Letzter Fund: {h['latest_finding']}")
        lines.append(f"  Naechster Schritt: {h['next_action']}")
    return lines


def _aufsichtsrat_lines(
    pending_approvals, duration_policy: dict, pending_stage_skips: int,
    stagnation_escalations: list = None, fix_md_new_entries: list = None,
    kaizen_new_count: int = 0,
) -> list:
    """"Fuer den Aufsichtsrat" only appears when something genuinely needs
    a human decision (section 8) - never printed out of habit. Triggers:
    open approvals, the section-6 duration-policy confirmation still
    pending, an open stage-skip escalation, a persistent stagnation
    escalation (section 3, stays until 'stagnation_ack: <id>' or a real
    build clears it), new unread FIX.md entries (FIX.md addendum Part 1.4 -
    short pointer only, never the full addendum text, resolve via
    'fix_resolved: <id>'), and new unread Kaizen board suggestions (Part
    2.3 - same short-pointer/dedup pattern, full text lives in
    kaizen_suggestions.jsonl only).
    """
    items = []
    if isinstance(pending_approvals, int) and pending_approvals > 0:
        items.append(f"- {pending_approvals} offene Freigabe(n) in der Queue (approve.py / Telegram-Reply).")
    if duration_policy and duration_policy.get("status") == "proposed":
        items.append(
            "- Duration-Policy-Vorschlag wartet auf Bestaetigung: "
            f"{duration_policy.get('values')}. Antworte 'duration_policy: confirm', oder "
            "'duration_policy: <research> <community_engagement> <landing_page> <build>' "
            "(Tage, 'none' fuer build) zum Anpassen."
        )
    if pending_stage_skips:
        items.append(f"- {pending_stage_skips} offene Stage-Skip-Anfrage(n) warten auf Main-CEO-Review.")
    for sub_id in (stagnation_escalations or []):
        items.append(
            f"- {sub_id}: seit {holding.STAGNATION_ESCALATION_THRESHOLD}+ aufeinanderfolgenden Zyklen "
            f"kein 'build', trotz {holding.STALL_RESOLVED_THRESHOLD}+ aufgeloester Hypothesen - "
            "bleibt hier stehen, bis 'stagnation_ack: " + sub_id + "' bestaetigt wird oder ein echter "
            "Build den Trend durchbricht."
        )
    if fix_md_new_entries:
        items.append(f"- FIX.md aktualisiert ({len(fix_md_new_entries)} neue Eintraege):")
        for e in fix_md_new_entries:
            items.append(f"  - [{e.get('category')}] {e.get('headline')} (fix_resolved: {e.get('id')})")
    if kaizen_new_count:
        items.append(
            f"- {kaizen_new_count} neue Kaizen-Vorschlaege fuers Board seit letztem Report, "
            "siehe kaizen_suggestions.jsonl."
        )
    if not items:
        return []
    return ["", "--- Fuer den Aufsichtsrat ---"] + items


def _technical_status_message(
    subsidiary_id: str, usage: dict, kickoff_error: Exception, persistence_warning: str,
    telegram_action_log: list, fix_md_new_entries: list,
) -> str:
    """Message A (cycle-reporting/backlog addendum, Part 1.1): short by
    default, grows only when something is actually wrong - on a clean
    cycle this is exactly 3 lines (header, usage headline, health
    confirmation), nothing more. Everything else here is conditional on a
    real triggered problem, never printed out of habit.
    """
    lines = [
        f"{subsidiary_id} Zyklus - {datetime.now(timezone.utc).isoformat()}",
        _usage_headline(usage),
    ]
    problem = False
    if kickoff_error is not None:
        lines.append(f"WARNUNG: Der Crew-Lauf ist fehlgeschlagen: {kickoff_error}")
        lines.append("Nachfolgende Tasks haben moeglicherweise nicht mehr gelaufen.")
        problem = True
    if persistence_warning:
        lines.append(f"WARNUNG: Zustand vermutlich nicht persistent: {persistence_warning}")
        problem = True
    if _limit_hits:
        lines.append("WARNUNG: Sicherheitslimits diesen Zyklus ausgeloest:")
        lines.extend(f"- {hit}" for hit in _limit_hits)
        problem = True
    if _malformed_tool_calls:
        lines.append(
            f"Hinweis: {len(_malformed_tool_calls)}x fehlerhafter Tool-Aufruf diesen Zyklus "
            "(siehe usage_history.jsonl fuer Details)."
        )
        problem = True
    if fix_md_new_entries:
        lines.append(f"Hinweis: FIX.md aktualisiert ({len(fix_md_new_entries)} neue Eintraege).")
    if telegram_action_log:
        lines.append("Telegram-Kommandos diesen Zyklus verarbeitet:")
        lines.extend(f"- {entry}" for entry in telegram_action_log)
    if not problem:
        lines.append("System gesund: kein Crash, Zustand persistent, keine Sicherheitslimits ausgeloest.")
    return "\n".join(lines)


def _kaizen_business_lines(subsidiary_id: str, since_iso: str) -> list:
    """Part 1.1: self-improvement items, acted vs deferred (both mechanical
    - holding.read_kaizen_actions, not trusted to the agent's own prose)
    separated from what's proposed to the board (holding.read_kaizen_
    suggestions - the existing fuer_aufsichtsrat bucket).
    """
    actions = holding.read_kaizen_actions(subsidiary_id, since_iso)
    acted = [a for a in actions if a.get("status") == "acted"]
    deferred = [a for a in actions if a.get("status") == "deferred"]
    proposed = holding.read_kaizen_suggestions(subsidiary_id, since_iso)
    if not (acted or deferred or proposed):
        return []
    lines = ["", "--- Selbstverbesserung (Kaizen) ---"]
    if acted:
        lines.append(f"Direkt umgesetzt ({len(acted)}):")
        lines.extend(f"- {a['action']}" for a in acted)
    if deferred:
        lines.append(f"Zurueckgestellt ({len(deferred)}):")
        lines.extend(f"- {a['action']} ({a.get('deferred_reason')})" for a in deferred)
    if proposed:
        lines.append(
            f"Dem Board vorgeschlagen ({len(proposed)}): siehe 'Fuer den Aufsichtsrat' unten."
        )
    return lines


def _approvals_business_lines(subsidiary_id: str, previously_reported_ids: list) -> tuple:
    """Part 1.1: open approval requests, explicitly split into genuinely
    new since the last report vs. already-known-and-still-pending -
    mechanical (list_pending_approval_ids vs. the prior business report's
    own reported_approval_ids), never re-presented as new when it isn't.
    Returns (lines, current_pending_ids) - the ids are what gets persisted
    via save_business_report for the NEXT cycle's comparison.
    """
    current_ids = list_pending_approval_ids(subsidiary_id)
    previously_reported = set(previously_reported_ids or [])
    new_ids = [i for i in current_ids if i not in previously_reported]
    known_ids = [i for i in current_ids if i in previously_reported]
    lines = []
    if current_ids:
        lines = ["", "--- Offene Freigaben ---"]
        if new_ids:
            lines.append(f"Neu seit letztem Report ({len(new_ids)}): {', '.join(new_ids)}")
        if known_ids:
            lines.append(f"Bereits bekannt, weiter offen ({len(known_ids)}): {', '.join(known_ids)}")
    return lines, current_ids


def _top_hypotheses_lines(block: dict) -> list:
    """Part 1.3, revised (report-verification addendum): two separate
    sections, not one merged ranking - "Aktuell in Testung" (every active
    hypothesis, always shown regardless of score - real ongoing validation
    work) and "Top-Backlog-Kandidaten" (backlog only, ranked by ice_score).
    Kept apart because an active hypothesis's own score has no enforced
    1-10 range unlike backlog ICE sub-scores, so merging them into one
    sorted list would compare two differently-scaled numbers - on top of
    real, in-flight work otherwise reading as less important than an
    untested paper idea purely because ICE's wider range dwarfs it.
    """
    if not block["active"] and not block["top_backlog"] and not block["new_this_cycle"]:
        return []
    lines = ["", "--- Aktuell in Testung ---"]
    if block["active"]:
        for c in block["active"]:
            marker = " (neu)" if c.get("is_new") else ""
            score = f"{c['score']:.1f}" if c.get("score") is not None else "n/a"
            lines.append(f"- {c['id']}{marker} [eigener Score={score}]: {c['one_liner']}")
    else:
        lines.append("Keine aktiven Hypothesen.")
    lines += ["", "--- Top-Backlog-Kandidaten ---"]
    if block["top_backlog"]:
        for i, c in enumerate(block["top_backlog"], 1):
            marker = " (neu)" if c.get("is_new") else ""
            score = f"{c['score']:.1f}" if c.get("score") is not None else "n/a"
            lines.append(f"{i}. {c['id']}{marker} [ice_score={score}]: {c['one_liner']}")
    else:
        lines.append("Backlog aktuell leer.")
    if block["new_this_cycle"]:
        lines.append("Weitere, neu diesen Zyklus (noch nicht oben gelistet):")
        for c in block["new_this_cycle"]:
            score = f"{c['score']:.1f}" if c.get("score") is not None else "n/a"
            lines.append(f"- {c['id']} [{c['status']}, score={score}]: {c['one_liner']}")
    return lines


def send_cycle_summary(
    subsidiary_id: str, kickoff_error: Exception = None, telegram_action_log: list = None,
    persistence_warning: str = None, spare_capacity_produced_nothing: bool = False,
) -> None:
    """Post two sequential Telegram messages for this cycle (cycle-
    reporting/backlog addendum, Part 1.1): Message A, a short technical
    status (this replaces the prior single combined message's cost/
    warnings preamble), and Message B, a Sub-CEO-to-board business
    narrative. Saves a condensed version of Message B as next cycle's
    channel-strategy continuity note, and persists a structured business-
    report record (next step, reported approval ids) for the NEXT cycle's
    own comparison. Called once after kickoff() finishes (or fails) -
    never raises itself, so a broken notification never masks whatever
    kickoff() already did or didn't do. If kickoff_error is set, that's
    reported up front instead of being silently swallowed. persistence_
    warning (from check_state_persistence(), computed once at the very
    start of the cycle in __main__) gets the same visibility tier as the
    existing safety-limit warnings. spare_capacity_produced_nothing (Part
    2, section 2.4 - computed in __main__ via scoring.spare_capacity_
    produced_nothing) is flagged plainly in Message B as its own finding
    when true, a stronger and more immediate signal than the existing
    3-cycle zero_state_streak check.
    """
    try:
        notify_new_pending_approvals()
        usage = _compute_cycle_usage()
        duration_policy = json.loads(
            read_subsidiary_policies.run(subsidiary_id=subsidiary_id)
        ).get("max_duration_days_by_stage")
        pending_stage_skips = len(json.loads(read_stage_skip_requests.run(status="pending")))
        stagnation_escalations = [
            s["id"] for s in json.loads(read_subsidiaries.run()) if s.get("stagnation_escalated")
        ]
        fix_md_new_entries = holding.read_unnotified_fix_entries()
        if fix_md_new_entries:
            holding.mark_fix_entries_notified([e["id"] for e in fix_md_new_entries])
        kaizen_new_entries = holding.read_unnotified_kaizen_suggestions()
        if kaizen_new_entries:
            holding.mark_kaizen_suggestions_notified([e["id"] for e in kaizen_new_entries])

        message_a = _technical_status_message(
            subsidiary_id, usage, kickoff_error, persistence_warning, telegram_action_log, fix_md_new_entries,
        )

        since_iso = (_previous_business_report or {}).get("at") or ""
        next_step = get_and_clear_pending_next_step(default="(kein naechster Schritt gemeldet)")
        approvals_lines, current_approval_ids = _approvals_business_lines(
            subsidiary_id, (_previous_business_report or {}).get("reported_approval_ids")
        )

        lines_b = [
            f"{subsidiary_id} Business Update - {datetime.now(timezone.utc).isoformat()}",
            "",
            "--- Hypothesen-Uebersicht ---",
        ]
        lines_b += _format_hypothesis_overview(build_hypothesis_overview())
        lines_b += ["", _task_summary(task_ceo)[:2500]]
        lines_b += _top_hypotheses_lines(build_top_hypotheses_block())
        lines_b += _kaizen_business_lines(subsidiary_id, since_iso)
        lines_b += approvals_lines
        if spare_capacity_produced_nothing:
            lines_b += [
                "",
                "--- Anti-Stagnation-Hinweis ---",
                "Dieser Zyklus hatte freie aktive-Test-Kapazitaet (aktive Hypothesen < "
                f"{tools.MAX_ACTIVE_HYPOTHESES}) und hat keinen neuen persistenten Zustand erzeugt "
                "(keine neue/aktualisierte Hypothese, kein Backlog-Eintrag, kein Research-Fund, kein "
                "Content-Draft, keine Task-Order) - das ist selbst ein Befund, siehe Backlog-Addendum "
                "Abschnitt 2.4.",
            ]
        lines_b += [
            "",
            "--- Main-CEO: Holding-Review ---",
            _task_summary(task_main_ceo_review)[:1000],
            "",
            "--- Dev ---",
            _task_summary(task_dev)[:400],
        ]
        lines_b += _aufsichtsrat_lines(
            len(current_approval_ids), duration_policy, pending_stage_skips, stagnation_escalations,
            fix_md_new_entries=fix_md_new_entries, kaizen_new_count=len(kaizen_new_entries),
        )
        lines_b += ["", f"Naechster Schritt: {next_step}"]
        message_b = "\n".join(lines_b)

        send_telegram_message(message_a)
        send_telegram_message(message_b)
        save_cycle_note(message_b[:3000])
        save_business_report(next_step=next_step, reported_approval_ids=current_approval_ids)
    except Exception as exc:
        print(f"[api-sentinel] cycle summary failed (crew run itself was unaffected): {exc}")


if __name__ == "__main__":
    print("[api-sentinel] Autonomous Loop Started (Anthropic Claude)...")
    # Checked first, before anything else - a warning needs to reach
    # Telegram even if everything after this fails or gets skipped.
    persistence = check_state_persistence()
    if persistence["warning"]:
        print(f"[api-sentinel] WARNUNG: {persistence['warning']}")
    telegram_action_log = process_telegram_commands()
    paused, pause_note = is_system_paused()
    if paused:
        print(f"[api-sentinel] system paused ({pause_note}) - skipping this cycle.")
        skip_message = (
            f"API Sentinel Zyklus uebersprungen - System ist pausiert ({pause_note}). "
            "Sende 'start', um fortzufahren."
        )
        if persistence["warning"]:
            skip_message += f"\nWARNUNG: Zustand vermutlich nicht persistent: {persistence['warning']}"
        send_telegram_message(skip_message)
    else:
        # Structural-rebuild addendum, section 2: no longer assumes exactly
        # one subsidiary - loops crew.kickoff() once per active subsidiary
        # (today: just api-sentinel, so this is one iteration in practice).
        # tools.set_active_subsidiary() switches which subsidiary's data
        # STATE_DIR reads/writes resolve against; kickoff(inputs=...)
        # interpolates {subsidiary_id} placeholders already in the task
        # text (crewai's own interpolate_only). Per-cycle tracking globals
        # are reset between subsidiaries so one subsidiary's limit-hits/
        # token-usage never leaks into the next one's report within the
        # same process run. Known remaining gap, not fixed here (see README
        # chapter 15): task descriptions still carry some content baked in
        # once at module load (e.g. the prior cycle's continuity note,
        # task_channel_strategy's concrete channel candidates) that isn't
        # re-rendered per subsidiary - fine with the one subsidiary that
        # exists today, a real limitation once a second one is genuinely
        # operative.
        active_subsidiaries = json.loads(read_subsidiaries.run(status="active"))
        if not active_subsidiaries:
            active_subsidiaries = [{"id": "api-sentinel"}]
        for sub in active_subsidiaries:
            sub_id = sub["id"]
            tools.set_active_subsidiary(sub_id)
            _limit_hits.clear()
            _task_usage_log.clear()
            _malformed_tool_calls.clear()
            # Anti-stagnation addendum, Part 2 section 2.4: snapshot BEFORE
            # kickoff() so send_cycle_summary can tell whether this cycle had
            # unused active-testing capacity at the start AND produced no
            # new persisted state by the end - immediate, single-cycle,
            # unlike holding.check_zero_state_streak's 3-cycle window.
            pre_active_count = len(json.loads(read_hypotheses.run(status="active")))
            cycle_start_counts = snapshot_state_counts()
            try:
                crew.kickoff(inputs={"subsidiary_id": sub_id})
                print(f"[api-sentinel] Execution finished for '{sub_id}'.")
                run_fix_checks_for_subsidiary(sub_id)
                spare_capacity_produced_nothing = scoring.spare_capacity_produced_nothing(
                    pre_active_count, tools.MAX_ACTIVE_HYPOTHESES, cycle_start_counts, snapshot_state_counts(),
                )
                send_cycle_summary(
                    subsidiary_id=sub_id, telegram_action_log=telegram_action_log,
                    persistence_warning=persistence["warning"],
                    spare_capacity_produced_nothing=spare_capacity_produced_nothing,
                )
            except Exception as exc:
                print(f"[api-sentinel] crew.kickoff() failed for '{sub_id}': {exc}")
                run_fix_checks_for_subsidiary(sub_id)
                spare_capacity_produced_nothing = scoring.spare_capacity_produced_nothing(
                    pre_active_count, tools.MAX_ACTIVE_HYPOTHESES, cycle_start_counts, snapshot_state_counts(),
                )
                send_cycle_summary(
                    subsidiary_id=sub_id, kickoff_error=exc, telegram_action_log=telegram_action_log,
                    persistence_warning=persistence["warning"],
                    spare_capacity_produced_nothing=spare_capacity_produced_nothing,
                )
