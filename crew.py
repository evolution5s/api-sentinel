import json
import os
from datetime import datetime, timezone

from crewai import Agent, Crew, Process, Task, LLM
from crewai.tasks.conditional_task import ConditionalTask

from tools import (
    check_escalation,
    compare_channel_performance,
    evaluate_hypothesis,
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
    request_approval,
    save_cycle_note,
    send_telegram_message,
    write_channel,
    write_hypothesis,
)
from holding import (
    decide_pivot_proposal,
    file_cross_subsidiary_request,
    file_pivot_proposal,
    read_cross_subsidiary_requests,
    read_pivot_proposals,
    read_subsidiaries,
    register_subsidiary,
    resolve_cross_subsidiary_request,
    search_research_archive,
    set_subsidiary_status,
)

_previous_cycle_note = read_last_cycle_note()

# Anthropic API Key aus den Umgebungsvariablen prüfen
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("[Error] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# Anthropic Claude Sonnet 5 - pro Agent ein eigenes LLM mit eigenem max_tokens
# statt eines geteilten Werts fuer alle vier, weil die Agenten sich stark
# darin unterscheiden, wie viel sie tatsaechlich zu sagen haben:
#   - Sub-CEO/Main-CEO: echte Abwaegung (Hypothesen-Verdikte, Channel-Wahl,
#     Pivot-Entscheidungen) - bekommt mehr Budget und darf denken.
#   - Growth/Dev: mechanisches Reporting bzw. Content-Generierung nach
#     Vorgabe des CEO-Reports - kleineres, vorhersagbares Budget, kein
#     Denken noetig.
#
# max_tokens ist bei Claude Sonnet 5 eine gemeinsame Obergrenze fuer
# Denken + sichtbare Antwort - zu knapp bemessen kann das Denken das
# Budget auffressen und die eigentliche Antwort abschneiden (stop_reason
# "max_tokens"), ohne dass das wie ein Fehler aussieht.
#
# thinking-Konfiguration - verifiziert gegen die tatsaechlich installierte
# crewai-Version (1.15.9 lokal, 1.15.11 gepinnt in requirements.txt, beide
# geprueft): AnthropicThinkingConfig.type ist dort ein Literal["enabled",
# "disabled"] - "adaptive" (Sonnet 5s tatsaechlicher On-Modus laut
# Anthropic) wird von crewai's eigener Pydantic-Validierung abgelehnt,
# noch bevor ein API-Call passiert.
#
# thinking={"type": "disabled"} wurde hier bewusst wieder entfernt - das
# war die Ursache des "thinking.disabled.budget_tokens: Extra inputs are
# not permitted"-Fehlers, der jeden Cron-Lauf abgebrochen hat.
# Root Cause verifiziert: AnthropicThinkingConfig(type="disabled")
# .model_dump() liefert IMMER {"type": "disabled", "budget_tokens": None}
# mit, und crewai's _prepare_completion_params() serialisiert das ohne
# exclude_none=True in den API-Call - Anthropic lehnt budget_tokens als
# Feld unter type="disabled" komplett ab, auch als null. Ueber den
# oeffentlichen thinking=-Parameter laesst sich das nicht umgehen, da
# Pydantic jeden Input in genau dieses Modell validiert. Das ist ein
# crewai-Bug (bestaetigt in 1.15.9 und 1.15.11), keine Fehlkonfiguration
# hier - ein Upstream-Report waere angebracht.
#
# Konsequenz: thinking bleibt fuer alle vier Agenten unGESETZT. Sonnet 5
# laeuft dann laut Anthropic-Doku ohnehin automatisch adaptiv - fuer
# Sub-CEO/Main-CEO war das immer schon die Absicht; fuer Growth/Dev
# verliert das den zuvor angestrebten "kein Denken noetig"-Sparvorteil,
# aber ein zuverlaessig laufender Cron-Job hat Vorrang vor der
# Fein-Optimierung. Growth's max_tokens wurde deshalb von 1500 auf 3000
# angehoben - bei 1500 UND jetzt zwangsweise aktivem Thinking ist die
# Gefahr real, dass Denken das Budget auffrisst und die sichtbare Antwort
# abschneidet (stop_reason "max_tokens"), ohne dass das wie ein Fehler
# aussieht.
_ANTHROPIC_KWARGS = {"model": "anthropic/claude-sonnet-5", "api_key": ANTHROPIC_KEY}

growth_llm = LLM(max_tokens=3000, **_ANTHROPIC_KWARGS)
dev_llm = LLM(max_tokens=8000, **_ANTHROPIC_KWARGS)
ceo_llm = LLM(max_tokens=8000, **_ANTHROPIC_KWARGS)
main_ceo_llm = LLM(max_tokens=4000, **_ANTHROPIC_KWARGS)

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
        "every reach number comes from read_channel_metrics, never a guess."
    ),
    llm=growth_llm,
    tools=[request_approval, read_channel_metrics, read_channels, read_state, read_hypotheses],
    max_iter=30,
    max_execution_time=600,
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
        "action."
    ),
    llm=dev_llm,
    tools=[open_pull_request],
    max_iter=15,
    max_execution_time=300,
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
        "scores only via evaluate_hypothesis (never by mental arithmetic). "
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
        read_channels, write_channel,
        file_pivot_proposal, file_cross_subsidiary_request, search_research_archive,
    ],
    max_iter=50,
    max_execution_time=900,
    max_rpm=20,
    max_retry_limit=1,
    verbose=True,
)

main_ceo_agent = Agent(
    role="Main-CEO of the Open Claw Holding",
    goal=(
        "Steer the holding's subsidiaries strategically: review pivot "
        "proposals and cross-subsidiary requests from Sub-CEOs, manage the "
        "subsidiary registry (including the dormant-state lifecycle), and "
        "loop in the Aufsichtsrat for anything with real reach - never "
        "decide big-impact moves alone"
    ),
    backstory=(
        "Runs the holding above individual subsidiaries' Sub-CEOs. With only "
        "api-sentinel registered today, most cycles have nothing to review - "
        "that's expected, not a sign anything is broken. Never fabricates a "
        "decision just to have something to report; 'nothing pending this "
        "cycle' is a complete, valid answer. Instantiating a new subsidiary, "
        "deploying new agents, or connecting new external tools always goes "
        "through request_approval to the Aufsichtsrat first, no exceptions - "
        "register_subsidiary itself enforces this, but the same discipline "
        "applies to every judgment call this role makes."
    ),
    llm=main_ceo_llm,
    tools=[
        read_subsidiaries, register_subsidiary, set_subsidiary_status,
        read_pivot_proposals, decide_pivot_proposal,
        read_cross_subsidiary_requests, resolve_cross_subsidiary_request,
        search_research_archive, request_approval,
    ],
    max_iter=25,
    max_execution_time=600,
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
CYCLE_TOKEN_BUDGET = 1_000_000


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
        "Call read_state to see current signups, then read_hypotheses(status="
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
        "Plus a short cross-platform format comparison for the CEO to weigh."
    ),
    callback=_make_iteration_watchdog(growth_agent, "Growth"),
)

task_ceo = ConditionalTask(
    condition=_within_cycle_budget,
    description=(
        "Run the Build-Measure-Learn loop: "
        "0) Call read_state() first. If total_hypotheses is 0 (nothing has "
        "ever been written - the very first cycle ever, before any "
        "hypothesis existed to evaluate or follow up on), the loop has "
        "nothing to start from yet: formulate and write exactly one initial "
        "hypothesis via write_hypothesis to actually kick it off. Pick a "
        "channel from whichever the channel-strategy step above left as "
        "status='testing', size it with the same judgment you'd apply to "
        "any hypothesis (a concrete statement, category, "
        "landing_page_variant_id, failure_rate, success_rate, duration_days), "
        "and leave prior_hypothesis_id/prior_score unset since it has no "
        "predecessor. This step only ever fires once, when the system is "
        "completely empty - once any hypothesis exists, new ones only ever "
        "come from step 6 below as follow-ups to an evaluated one. "
        "1) Call read_hypotheses(status='active') and find any hypothesis "
        "whose duration_days has elapsed since created_at. "
        "2) For each due hypothesis, first make sure measured.reach_estimate "
        "is set (use the Growth report above; if it's still missing, leave "
        "the hypothesis active and note that it can't be scored yet). If it "
        "is set, call evaluate_hypothesis(hypothesis_id) to get the real score "
        "and verdict, then call write_hypothesis to persist status='evaluated', "
        "the score, and measured.conversions. "
        "3) If the verdict is 'inconclusive' and extension_used is false, "
        "instead extend the hypothesis once (write_hypothesis with a new "
        "duration_days and extension_used=true, status stays 'active') rather "
        "than closing it - never extend a second time. "
        "4) After evaluating, call check_escalation(hypothesis_id). If it "
        "returns escalate=true, this is a pivot-level decision, not "
        "something to decide or escalate to the board yourself: fill out "
        "the standard pivot template and call file_pivot_proposal("
        "subsidiary_id='api-sentinel', proposal=...) with all required "
        "fields (nature_of_change, validating_data, "
        "evolutionary_or_disruptive, existing_business_disposition, "
        "capability_gap_analysis, new_resources_needed, risk_assessment, "
        "synergy_overlap) - cite the real rolling-average score from "
        "check_escalation as your validating_data, never invent it. The "
        "Main-CEO reviews it next cycle; don't also file a separate "
        "request_approval for the same issue, and don't quietly pivot on "
        "your own instead. "
        "5) Pick the channel for any follow-up hypothesis only from "
        "whichever channels the channel-strategy step above left as "
        "status='testing' (write_hypothesis enforces this - it rejects a "
        "channel that isn't currently 'testing' in the roster). Within "
        "that set, weigh the Growth report's format comparison and each "
        "channel's real average score from compare_channel_performance() "
        "to decide which one fits this particular follow-up best - don't "
        "just repeat the same one out of habit if another testing channel "
        "fits the hypothesis better. Never pick a channel outside the "
        "current testing set yourself; if none of them fit, say so and "
        "leave the follow-up for next cycle instead of forcing it. "
        "6) Formulate exactly one follow-up hypothesis per evaluated "
        "hypothesis, setting prior_hypothesis_id, prior_score, and channel "
        "per the reasoning above, sized per the decision band the score "
        "fell into. Call write_hypothesis to create it - if it's rejected "
        "for hitting the parallelism limit on that landing_page_variant_id, "
        "pick a different variant or hold off. "
        "7) If the new hypothesis needs a new or changed landing page "
        "variant, say so explicitly in your final report so the Dev agent "
        "can act on it. Making any variant live is category 'publish' and "
        "needs request_approval - never skip that. "
        "8) Never invent conversion, reach, or revenue numbers. Every number "
        "in your report must trace back to a tool call above."
    ),
    agent=ceo_agent,
    expected_output=(
        "Status report: for each evaluated hypothesis, its score/verdict, "
        "what happened next (scaled/extended/pivoted/dropped), the new "
        "follow-up hypothesis started, and any pending approval or escalation "
        "filed. Pending Dev work (new variant needed or not) stated plainly."
    ),
    callback=_make_iteration_watchdog(ceo_agent, "Sub-CEO (Build-Measure-Learn)"),
)

task_main_ceo_review = ConditionalTask(
    condition=_within_cycle_budget,
    description=(
        "Run the holding's governance review for this cycle:\n"
        "1) Call read_pivot_proposals(status='pending'). For each: weigh it "
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
        "2) Call read_cross_subsidiary_requests(status='pending'). For "
        "each: decide whether it's justified and call "
        "resolve_cross_subsidiary_request. With only one subsidiary "
        "registered today there is usually nowhere to actually route the "
        "request to - approve or reject the request itself honestly, but "
        "never fabricate a result you can't actually produce; say plainly "
        "if no other subsidiary exists yet to fetch from.\n"
        "3) Call read_subsidiaries() and report the current holding "
        "structure (which are active/dormant). Only call "
        "set_subsidiary_status if there's a concrete reason to change one "
        "this cycle (e.g. a Sub-CEO reported its project done or paused) - "
        "never change status speculatively.\n"
        "4) Never call register_subsidiary without an already-approved "
        "request_approval backing it - the tool enforces this, but don't "
        "attempt it prematurely either.\n"
        "5) Nothing to review this cycle is a completely normal, valid "
        "outcome - report it as such rather than inventing busywork."
    ),
    agent=main_ceo_agent,
    expected_output=(
        "Pivot proposals reviewed (if any) with decisions and reasoning. "
        "Cross-subsidiary requests resolved (if any). Current subsidiary "
        "registry summary. Any request_approval filed for board sign-off."
    ),
    callback=_make_iteration_watchdog(main_ceo_agent, "Main-CEO"),
)

task_dev = ConditionalTask(
    condition=_within_cycle_budget,
    description=(
        "Read the CEO's report above. If and only if it says a new or changed "
        "landing page variant is needed, call open_pull_request to add it as "
        "a new file (naming pattern lp_v{n}_{label}.html) on a new branch "
        "against main - never edit index.html directly and never merge. If "
        "the CEO's report didn't ask for a variant this cycle, do nothing and "
        "say so."
    ),
    agent=dev_agent,
    expected_output="PR URL if one was opened, or a note that no variant was needed this cycle.",
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
