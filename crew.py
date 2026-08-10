import json
import os
from datetime import datetime, timezone
from pathlib import Path

from crewai import Agent, Crew, Process, Task, LLM
from crewai.events import crewai_event_bus
from crewai.events.types.tool_usage_events import ToolValidateInputErrorEvent
from crewai.tasks.conditional_task import ConditionalTask

import crewai_patches
import pricing

# Muss vor jeder Agent/Task-Konstruktion passieren, insbesondere vor dem
# ersten crew.kickoff() - siehe crewai_patches.py: crewai wirft einen
# "assistant message prefill"-400 (Anthropic lehnt jede Conversation ab,
# die auf einer assistant-Nachricht endet) IMMER, wenn ein Agent sein
# max_iter tatsaechlich erreicht - kein Rand-, sondern der Normalfall,
# sobald ein Cap wirklich mal greift. Reproduziert in Produktion.
crewai_patches.apply_patches()

from tools import (
    build_hypothesis_overview,
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
    is_system_paused,
    log_cycle_usage,
    log_research_finding,
    notify_new_pending_approvals,
    open_pull_request,
    process_telegram_commands,
    read_channel_metrics,
    read_channels,
    read_content_drafts,
    read_due_hypotheses,
    read_hypotheses,
    read_knowledge_base,
    read_last_cycle_note,
    read_research_findings,
    read_state,
    read_task_orders,
    request_approval,
    save_cycle_note,
    send_telegram_message,
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
    file_pivot_proposal,
    file_stage_skip_request,
    file_status_report,
    propose_idea,
    read_cross_subsidiary_requests,
    read_ideas,
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
        "Telegram 'posted:' reply. Every reach number comes from "
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
        "into work here unasked."
    ),
    llm=growth_llm,
    tools=[
        request_approval, read_channel_metrics, read_channels, read_state, read_hypotheses,
        read_task_orders, complete_task_order, draft_content, read_content_drafts,
        check_community_risk, get_account_stats, log_research_finding, read_research_findings,
        read_subsidiary_policies, read_knowledge_base, propose_idea,
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
        "use up."
    ),
    llm=dev_llm,
    tools=[open_pull_request, read_task_orders, complete_task_order, check_approval_status],
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
        "of chasing it here unasked."
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
        "that decision belongs to the board, not this role or the Sub-CEO."
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


def _within_cycle_budget(_previous_task_output) -> bool:
    total = crew.calculate_usage_metrics().total_tokens
    if total >= CYCLE_TOKEN_BUDGET:
        _limit_hits.append(
            f"Zyklus-Token-Budget ({CYCLE_TOKEN_BUDGET:,}) erreicht "
            f"({total:,} verbraucht) - verbleibende Tasks diesen Zyklus uebersprungen"
        )
        return False
    return True


# Tasks definieren
task_channel_strategy = Task(
    description=(
        "Maintain the traction-channel roster using the Bullseye framework "
        "(Traction, Weinberg & Mares): brainstorm candidate channels, score "
        "them, test a small number in parallel, double down on whichever "
        "wins, and swap out ones that stop working instead of grinding on "
        "them. This runs before any hypothesis is picked or created - it "
        "decides which channels are even in play this cycle.\n"
        "0) Call read_strategic_direction(subsidiary_id='api-sentinel') "
        "first. No direction set is a normal, valid state - most cycles "
        "will have none. If one is set, read it as the frame for this "
        "cycle's channel and hypothesis judgment calls, not as a command "
        "that overrides your own tactical read of the data below. Also call "
        "read_subsidiary_policies(subsidiary_id='api-sentinel') - if "
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
        "landing_page_direct (real analytics) as a reach signal.\n"
        "2) Look at whichever channels are currently status='testing' (max "
        "3, enforced by write_channel). For each with enough evaluated "
        "hypotheses to trust the number, call compare_channel_performance() "
        "and check its real average score. If it's stopped producing decent "
        "scores, move it to status='bench' or 'retired' via write_channel "
        "with a concrete reason (cite the average score and hypothesis "
        "count), and promote a different not_tested/bench candidate into "
        "'testing' instead - report this swap explicitly, it changes "
        "direction just like a hypothesis pivot does.\n"
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
    condition=_within_cycle_budget,
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
        "make yourself.\n"
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
    condition=_within_cycle_budget,
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
        "was found. evidence_stage='community_engagement' requires a real "
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
        "read_research_findings(hypothesis_id) for anything already logged "
        "- a competitor product, a forum discussion, replies to a genuine "
        "question Growth posted - and log_research_finding yourself for "
        "anything relevant you already know, specific to this hypothesis "
        "(at least RESEARCH_FINDING_MIN_LENGTH characters, a real artifact - "
        "which threads/posts, what they said, how many, how recent, or an "
        "equally specific honest negative result - never a one-liner claim, "
        "log_research_finding rejects those outright). This research-"
        "evidence tier is cheaper and faster than a live experiment and the "
        "default first step now (evidence_stage='research', see the fields-"
        "required guidance above) - it is weaker evidence than a live "
        "experiment: it can support 'test_further'/'pivot' reasoning, never "
        "a 'build' outcome on its own - only evaluate_hypothesis's real "
        "score from actual signups does that. For a hypothesis where real "
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
        "research plan, once actual due diligence justifies it. Then close "
        "out anything still open and tied to it: call read_task_orders"
        "(to_role='dev')/read_task_orders(to_role='growth') and "
        "complete_task_order(order_id, result=\"cancelled - hyp_bootstrap_"
        "001 buried, see bury_reasoning\") for every order still "
        "status='open' with hypothesis_id='hyp_bootstrap_001' - don't leave "
        "orphaned orders pointing at a buried hypothesis. Also call "
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
        "file_pivot_proposal(subsidiary_id='api-sentinel', proposal=...) "
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
        "7) File a file_status_report(subsidiary_id='api-sentinel', ...) "
        "to the Main-CEO for every hypothesis you evaluated this cycle - "
        "what was being tested, what you found, and the outcome. Set "
        "needs_decision_from_above=true with a concrete decision_context "
        "for every 'build' outcome (that approval request needs the "
        "Main-CEO's/board's attention) and for anything else that "
        "genuinely needs a call from above; false is a normal, valid "
        "answer for a routine bury/pivot/test_further.\n"
        "8) Never invent conversion, reach, revenue, or economics numbers. "
        "Every number in your report must trace back to a tool call above."
    ),
    agent=ceo_agent,
    expected_output=(
        "Status report: for each evaluated hypothesis, its score/outcome "
        "(build/test_further/pivot/bury) and the economics behind it, what "
        "happened next, and the new retest started for any pivot. Any "
        "knowledge_base entries written for a resolved hypothesis. Any "
        "pending approval, escalation, or status report filed for the "
        "Main-CEO. Pending Dev work stated as task orders filed, not just "
        "narrated."
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
        "1) Call read_status_reports(subsidiary_id='api-sentinel', "
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
        "Any request_approval filed for board sign-off."
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
        "as a new file (naming pattern lp_v{n}_{label}.html) on a new "
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


def _aufsichtsrat_lines(pending_approvals, duration_policy: dict, pending_stage_skips: int) -> list:
    """"Fuer den Aufsichtsrat" only appears when something genuinely needs
    a human decision (section 8) - never printed out of habit. Three
    concrete triggers: open approvals, the section-6 duration-policy
    confirmation still pending, or an open stage-skip escalation.
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
    if not items:
        return []
    return ["", "--- Fuer den Aufsichtsrat ---"] + items


def send_cycle_summary(
    kickoff_error: Exception = None, telegram_action_log: list = None,
    persistence_warning: str = None,
) -> None:
    """Post a what-happened/what's-next digest of this cycle to Telegram as
    a single message (structural-rebuild addendum, section 8 - the prior
    second "formatted cost table" message was fully redundant with
    _usage_headline()/_usage_detail_line() already below, same numbers
    re-tabulated, so dropping it loses no information and removes a real
    Markdown-parse-failure risk rather than trading one risk for another),
    and saves a condensed version as next cycle's continuity note. Called
    once after kickoff() finishes (or fails) - never raises itself, so a
    broken notification never masks whatever kickoff() already did or
    didn't do. If kickoff_error is set, that's reported up front instead of
    being silently swallowed - a hard crew failure must still reach
    Telegram, not just Railway's logs, since that's the only place a human
    reliably sees it. persistence_warning (from check_state_persistence(),
    computed once at the very start of the cycle in __main__) gets the same
    visibility tier as the existing budget/max_iter warnings below - state
    silently not surviving the next redeploy is exactly the kind of thing
    that must never go unnoticed again (see README chapter 15).
    """
    try:
        notify_new_pending_approvals()
        pending = json.loads(read_state.run()).get("pending_approvals", "?")
        usage = _compute_cycle_usage()
        hypothesis_overview = build_hypothesis_overview()
        duration_policy = json.loads(
            read_subsidiary_policies.run(subsidiary_id="api-sentinel")
        ).get("max_duration_days_by_stage")
        pending_stage_skips = len(json.loads(read_stage_skip_requests.run(status="pending")))
        # Total tokens is the single most-glanced-at number in this report -
        # kept as its own standalone line right at the top, ahead of even
        # the profile/model detail line below, so it's unmissable rather
        # than folded mid-sentence into a longer paragraph.
        lines = [
            f"API Sentinel Zyklus - {datetime.now(timezone.utc).isoformat()}",
            _usage_headline(usage),
            "",
            "--- Hypothesen-Uebersicht ---",
        ]
        lines += _format_hypothesis_overview(hypothesis_overview)
        if kickoff_error is not None:
            lines.append(f"WARNUNG: Der Crew-Lauf ist fehlgeschlagen: {kickoff_error}")
            lines.append("Nachfolgende Tasks haben moeglicherweise nicht mehr gelaufen.")
        if telegram_action_log:
            lines.append("Telegram-Kommandos diesen Zyklus verarbeitet:")
            lines.extend(f"- {entry}" for entry in telegram_action_log)
        lines += [
            _usage_detail_line(usage),
            f"Offene Freigaben (approve.py / Telegram-Reply): {pending}",
        ]
        if persistence_warning:
            lines.append(f"WARNUNG: Zustand vermutlich nicht persistent: {persistence_warning}")
        if _limit_hits:
            lines.append("WARNUNG: Sicherheitslimits diesen Zyklus ausgeloest:")
            lines.extend(f"- {hit}" for hit in _limit_hits)
        if _malformed_tool_calls:
            lines.append(
                f"Hinweis: {len(_malformed_tool_calls)}x fehlerhafter Tool-Aufruf diesen Zyklus "
                "(leeres/unvollstaendiges Argument-Dict vor der eigenen Validierung, vermuteter "
                "Zusammenhang mit dem strict-tools-Patch - siehe usage_history.jsonl fuer Details)."
            )
        if _task_usage_log:
            per_task = ", ".join(f"{e['task']}={e['tokens']}" for e in _task_usage_log)
            lines.append(f"Tokens pro Task: {per_task}")
        lines += [
            "",
            "--- Channel-Strategie ---",
            _task_summary(task_channel_strategy)[:1200],
            "",
            "--- Wachstum ---",
            _task_summary(task_growth)[:800],
            "",
            "--- Sub-CEO (API Sentinel): Ergebnis & naechste Schritte ---",
            _task_summary(task_ceo)[:1800],
            "",
            "--- Main-CEO: Holding-Review ---",
            _task_summary(task_main_ceo_review)[:1000],
            "",
            "--- Dev ---",
            _task_summary(task_dev)[:400],
        ]
        lines += _aufsichtsrat_lines(pending, duration_policy, pending_stage_skips)
        full_summary = "\n".join(lines)
        send_telegram_message(full_summary)
        save_cycle_note(full_summary[:3000])
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
        try:
            crew.kickoff()
            print("[api-sentinel] Execution finished.")
            send_cycle_summary(telegram_action_log=telegram_action_log, persistence_warning=persistence["warning"])
        except Exception as exc:
            print(f"[api-sentinel] crew.kickoff() failed: {exc}")
            send_cycle_summary(
                kickoff_error=exc, telegram_action_log=telegram_action_log,
                persistence_warning=persistence["warning"],
            )
