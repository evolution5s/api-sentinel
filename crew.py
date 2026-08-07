import json
import os
from datetime import datetime, timezone
from pathlib import Path

from crewai import Agent, Crew, Process, Task, LLM
from crewai.tasks.conditional_task import ConditionalTask

import crewai_patches

# Muss vor jeder Agent/Task-Konstruktion passieren, insbesondere vor dem
# ersten crew.kickoff() - siehe crewai_patches.py: crewai wirft einen
# "assistant message prefill"-400 (Anthropic lehnt jede Conversation ab,
# die auf einer assistant-Nachricht endet) IMMER, wenn ein Agent sein
# max_iter tatsaechlich erreicht - kein Rand-, sondern der Normalfall,
# sobald ein Cap wirklich mal greift. Reproduziert in Produktion.
crewai_patches.apply_patches()

from tools import (
    check_approval_status,
    check_escalation,
    compare_channel_performance,
    complete_task_order,
    compute_break_even,
    evaluate_hypothesis,
    file_task_order,
    is_system_paused,
    log_cycle_usage,
    notify_new_pending_approvals,
    open_pull_request,
    process_telegram_commands,
    read_channel_metrics,
    read_channels,
    read_hypotheses,
    read_last_cycle_note,
    read_state,
    read_task_orders,
    request_approval,
    save_cycle_note,
    send_telegram_message,
    write_channel,
    write_hypothesis,
)
from holding import (
    acknowledge_status_report,
    decide_pivot_proposal,
    file_cross_subsidiary_request,
    file_pivot_proposal,
    file_status_report,
    read_cross_subsidiary_requests,
    read_pivot_proposals,
    read_status_reports,
    read_strategic_direction,
    read_subsidiaries,
    register_subsidiary,
    resolve_cross_subsidiary_request,
    search_research_archive,
    set_strategic_direction,
    set_subsidiary_status,
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
    goal="Draft content matching the currently active hypothesis and measure real reach after it's approved and posted",
    backstory=(
        "Technical marketer for the Freqtrade/CCXT quant-bot community. Drafts "
        "posts and measures results, but has no publishing authority of its own "
        "- every piece of content goes through request_approval first, and "
        "every reach number comes from read_channel_metrics, never a guess. "
        "Concrete work comes from the Sub-CEO as task orders "
        "(read_task_orders(to_role='growth', status='open')) - that's the "
        "authoritative ask, not a paraphrase of it; call complete_task_order "
        "with the real result when done, don't just narrate it in prose."
    ),
    llm=growth_llm,
    tools=[
        request_approval, read_channel_metrics, read_channels, read_state, read_hypotheses,
        read_task_orders, complete_task_order,
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
        "complete_task_order with the real result (e.g. the PR URL) when done."
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
        "Sentinel into a profitable bootstrapped business - without ever "
        "fabricating a number, bypassing the human approval queue, or deciding "
        "a fundamental strategy change alone"
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
        "effort. Reads the Main-CEO's current strategic direction "
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
        "the Main-CEO (file_cross_subsidiary_request)."
    ),
    llm=ceo_llm,
    tools=[
        read_state, read_hypotheses, write_hypothesis, evaluate_hypothesis,
        check_escalation, compare_channel_performance, request_approval,
        read_channels, write_channel, compute_break_even,
        file_task_order, read_task_orders,
        file_status_report, read_strategic_direction,
        file_pivot_proposal, file_cross_subsidiary_request, search_research_archive,
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
        "Steer the holding's subsidiaries strategically: review pivot "
        "proposals, cross-subsidiary requests, and status reports from "
        "Sub-CEOs, set strategic direction where it's actually warranted, "
        "manage the subsidiary registry (including the dormant-state "
        "lifecycle), and loop in the Aufsichtsrat for anything with real "
        "reach - never decide big-impact moves alone"
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
        "(acknowledge_status_report) so they don't keep resurfacing. Can set "
        "a Sub-CEO's strategic direction (set_strategic_direction) when "
        "there's a real reason to - a market shift, a pattern across several "
        "reports, a decision that just got made - but this is the exception, "
        "not a box to fill every cycle; it doesn't override the Sub-CEO's own "
        "tactical judgment, it's the frame the Sub-CEO reads and works "
        "within. Instantiating a new subsidiary, deploying new agents, or "
        "connecting new external tools always goes through request_approval "
        "to the Aufsichtsrat first, no exceptions - register_subsidiary "
        "itself enforces this, but the same discipline applies to every "
        "judgment call this role makes."
    ),
    llm=main_ceo_llm,
    tools=[
        read_subsidiaries, register_subsidiary, set_subsidiary_status,
        read_pivot_proposals, decide_pivot_proposal,
        read_cross_subsidiary_requests, resolve_cross_subsidiary_request,
        read_status_reports, acknowledge_status_report, set_strategic_direction,
        search_research_archive, request_approval,
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
# --------------------------------------------------------------------------
_limit_hits: list[str] = []


def _make_iteration_watchdog(agent: Agent, label: str):
    def _watchdog(_output):
        executor = agent.agent_executor
        if executor is not None and executor.iterations >= agent.max_iter:
            _limit_hits.append(f"{label}: max_iter-Kappe ({agent.max_iter}) erreicht, finale Antwort erzwungen")
    return _watchdog


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
        "that overrides your own tactical read of the data below.\n"
        "1) Call read_channels() to see the current roster. If it's empty, "
        "brainstorm a first set of candidate channels for this niche "
        "(Freqtrade/CCXT quant-bot users) and write each one with "
        "write_channel. If it already has entries - including a partial set "
        "from an earlier interrupted attempt - do NOT brainstorm a fresh "
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
        "4) A paid channel (is_paid=true) needs an approved spend request "
        "before it can become 'testing' - reuses the existing approval "
        "queue, no separate gate. Weigh paid spend honestly using real "
        "numbers, not enthusiasm: Reddit's own $5/day minimum only buys "
        "2-3 clicks, nowhere near a signal; a test that actually produces "
        "learnable data realistically runs $1,500-3,000 over 2-3 weeks; "
        "and below roughly $1,000/month, organic channels have generally "
        "outperformed paid ones by a wide margin for early-stage products. "
        "This is input to your scoring, not a rule that excludes paid "
        "channels outright - if you still think it's worth testing, file "
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
        "make yourself. "
        "Do not draft or post new content, and never propose or place paid "
        "ad spend yourself - both go through request_approval and are only "
        "acted on by a human after approval. Check the state, don't assume "
        "something was already approved."
    ),
    agent=growth_agent,
    expected_output=(
        "Per active hypothesis: estimated_reach, reach_source, fetch_note if any. "
        "Plus a short cross-platform format comparison for the CEO to weigh. "
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
        "already validated as valuable) and its own economics fixed BEFORE "
        "it runs, never adjusted afterward to fit the result: "
        "estimated_build_cost (rough token/time cost of the real product/"
        "feature this would become if it succeeds - not the cost of the "
        "test itself), price_point_monthly, and break_even_horizon_months. "
        "Call compute_break_even(estimated_build_cost, price_point_monthly, "
        "break_even_horizon_months) to get break_even_users - never estimate "
        "that number yourself - and include it on the write_hypothesis call. "
        "A landing page costing a few dollars to build has a very different "
        "bar than something needing real Dev-agent effort - size the "
        "economics honestly per hypothesis, there is no one fixed target.\n"
        "0) Call read_hypotheses() with no filter first. If it's completely "
        "empty (nothing has ever been written - the very first cycle ever), "
        "the loop has nothing to start from yet: formulate and write exactly "
        "one initial hypothesis via write_hypothesis to actually kick it "
        "off, including hypothesis_type and the economics above. Pick a "
        "channel from whichever the channel-strategy step above left as "
        "status='testing', size it with the same judgment you'd apply to "
        "any hypothesis (a concrete statement, category, "
        "landing_page_variant_id, failure_rate, success_rate, duration_days), "
        "and leave prior_hypothesis_id/prior_score unset since it has no "
        "predecessor. This step only ever fires once, when the system is "
        "completely empty - once any hypothesis exists, new ones only ever "
        "come from step 5 below, as a pivot follow-up to an evaluated one.\n"
        "1) Call read_hypotheses(status='active') and find any hypothesis "
        "whose duration_days has elapsed since created_at.\n"
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
        "outcome='build', the score, and measured.conversions. This is the "
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
        "economics (hypothesis_type, estimated_build_cost, "
        "price_point_monthly, break_even_horizon_months, and a fresh "
        "compute_break_even call for break_even_users) rather than copying "
        "the prior hypothesis's numbers unchecked. Call write_hypothesis to "
        "create it - if it's rejected for hitting the parallelism limit on "
        "that landing_page_variant_id, pick a different variant or hold off. "
        "Pivot attempts on one lineage are capped (evaluate_hypothesis "
        "enforces this automatically by returning 'bury' once the cap is "
        "hit) - don't try to talk a hypothesis that's already spent its "
        "pivot budget into one more retest.\n"
        "6) If a pivot retest needs a new or changed landing page variant, "
        "file a file_task_order(to_role='dev', ...) for it explicitly - "
        "don't just mention it in your report and assume Dev will notice. "
        "Making any variant live is category 'publish' and needs "
        "request_approval - never skip that, on top of the 'deploy' "
        "approval already required for build outcomes above.\n"
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
        "5) Only if the status reports above show a genuine reason to - a "
        "pattern across several of them, a market shift, a decision that "
        "just got made - call set_strategic_direction(subsidiary_id="
        "'api-sentinel', focus_area=..., reasoning=...) to steer the "
        "Sub-CEO's focus next cycle. This is the exception, not something "
        "to do every cycle just to have done it - most cycles should set no "
        "new direction, and that's a completely valid outcome too.\n"
        "6) Never call register_subsidiary without an already-approved "
        "request_approval backing it - the tool enforces this, but don't "
        "attempt it prematurely either.\n"
        "7) Nothing to review this cycle is a completely normal, valid "
        "outcome - report it as such rather than inventing busywork."
    ),
    agent=main_ceo_agent,
    expected_output=(
        "Status reports reviewed (if any needed a decision) with what was "
        "decided. Pivot proposals reviewed (if any) with decisions and "
        "reasoning. Cross-subsidiary requests resolved (if any). Current "
        "subsidiary registry summary. Any strategic direction set (or "
        "explicitly not set, and why not). Any request_approval filed for "
        "board sign-off."
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


def _usage_line() -> str:
    metrics = getattr(crew, "usage_metrics", None)
    if metrics is None:
        return "LLM-Nutzung: nicht verfuegbar"
    usage = {
        "total_tokens": getattr(metrics, "total_tokens", None),
        "prompt_tokens": getattr(metrics, "prompt_tokens", None),
        "cached_prompt_tokens": getattr(metrics, "cached_prompt_tokens", None),
        "cache_creation_tokens": getattr(metrics, "cache_creation_tokens", None),
        "completion_tokens": getattr(metrics, "completion_tokens", None),
        "successful_requests": getattr(metrics, "successful_requests", None),
    }
    log_cycle_usage(usage)
    return (
        f"Agent-Profil: '{AGENT_PROFILE['name']}' ({AGENT_PROFILE['model']}). "
        f"LLM-Nutzung diesen Zyklus: {usage['total_tokens']} tokens gesamt "
        f"({usage['prompt_tokens']} prompt, {usage['completion_tokens']} completion), "
        f"Prompt-Cache: {usage['cached_prompt_tokens']} tokens gelesen "
        f"(guenstig), {usage['cache_creation_tokens']} tokens neu geschrieben "
        f"(teurer, einmalig pro Cache-Fenster) - CrewAI cached role/goal/"
        f"backstory + Tool-Definitionen pro Agent automatisch, sobald der "
        f"gemeinsame Praefix (Tools+System) das Modell-Minimum erreicht; "
        f"{usage['successful_requests']} Requests, "
        f"{usage['total_tokens']}/{CYCLE_TOKEN_BUDGET:,} Zyklus-Budget "
        f"({round(100 * (usage['total_tokens'] or 0) / CYCLE_TOKEN_BUDGET)}%); "
        f"max_tokens/Call: "
        f"Growth={growth_llm.max_tokens} Dev={dev_llm.max_tokens} "
        f"Sub-CEO={ceo_llm.max_tokens} Main-CEO={main_ceo_llm.max_tokens}"
    )


def send_cycle_summary(kickoff_error: Exception = None, telegram_action_log: list = None) -> None:
    """Post a what-happened/what's-next digest of this cycle to Telegram,
    and save a condensed version as next cycle's continuity note. Called
    once after kickoff() finishes (or fails) - never raises itself, so a
    broken notification never masks whatever kickoff() already did or
    didn't do. If kickoff_error is set, that's reported up front instead of
    being silently swallowed - a hard crew failure must still reach Telegram,
    not just Railway's logs, since that's the only place a human reliably
    sees it.
    """
    try:
        notify_new_pending_approvals()
        pending = json.loads(read_state.run()).get("pending_approvals", "?")
        lines = [f"API Sentinel Zyklus - {datetime.now(timezone.utc).isoformat()}"]
        if kickoff_error is not None:
            lines.append(f"WARNUNG: Der Crew-Lauf ist fehlgeschlagen: {kickoff_error}")
            lines.append("Nachfolgende Tasks haben moeglicherweise nicht mehr gelaufen.")
        if telegram_action_log:
            lines.append("Telegram-Kommandos diesen Zyklus verarbeitet:")
            lines.extend(f"- {entry}" for entry in telegram_action_log)
        lines += [
            _usage_line(),
            f"Offene Freigaben (approve.py / Telegram-Reply): {pending}",
        ]
        if _limit_hits:
            lines.append("WARNUNG: Sicherheitslimits diesen Zyklus ausgeloest:")
            lines.extend(f"- {hit}" for hit in _limit_hits)
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
        full_summary = "\n".join(lines)
        send_telegram_message(full_summary)
        save_cycle_note(full_summary[:3000])
    except Exception as exc:
        print(f"[api-sentinel] cycle summary failed (crew run itself was unaffected): {exc}")


if __name__ == "__main__":
    print("[api-sentinel] Autonomous Loop Started (Anthropic Claude)...")
    telegram_action_log = process_telegram_commands()
    paused, pause_note = is_system_paused()
    if paused:
        print(f"[api-sentinel] system paused ({pause_note}) - skipping this cycle.")
        send_telegram_message(
            f"API Sentinel Zyklus uebersprungen - System ist pausiert ({pause_note}). "
            "Sende 'start', um fortzufahren."
        )
    else:
        try:
            crew.kickoff()
            print("[api-sentinel] Execution finished.")
            send_cycle_summary(telegram_action_log=telegram_action_log)
        except Exception as exc:
            print(f"[api-sentinel] crew.kickoff() failed: {exc}")
            send_cycle_summary(kickoff_error=exc, telegram_action_log=telegram_action_log)
