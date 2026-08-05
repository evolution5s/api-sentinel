import os
from crewai import Agent, Crew, Process, Task, LLM

from tools import (
    check_escalation,
    compare_channel_performance,
    evaluate_hypothesis,
    open_pull_request,
    read_channel_metrics,
    read_channels,
    read_hypotheses,
    read_state,
    request_approval,
    write_channel,
    write_hypothesis,
)

# Anthropic API Key aus den Umgebungsvariablen prüfen
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("[Error] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# Anthropic Claude Sonnet 5 als dediziertes LLM definieren
claude_llm = LLM(
    model="anthropic/claude-sonnet-5",
    api_key=ANTHROPIC_KEY
)

# Agents mit dem Claude-LLM konfigurieren
growth_agent = Agent(
    role="Growth Engine / Dev Relations",
    goal="Draft content matching the currently active hypothesis and measure real reach after it's approved and posted",
    backstory=(
        "Technical marketer for the Freqtrade/CCXT quant-bot community. Drafts "
        "posts and measures results, but has no publishing authority of its own "
        "- every piece of content goes through request_approval first, and "
        "every reach number comes from read_channel_metrics, never a guess."
    ),
    llm=claude_llm,
    tools=[request_approval, read_channel_metrics, read_channels],
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
    llm=claude_llm,
    tools=[open_pull_request],
    verbose=True,
)

ceo_agent = Agent(
    role="Autonomous CEO & Lean Startup strategist",
    goal=(
        "Evaluate due hypotheses, formulate follow-up hypotheses, and grow API "
        "Sentinel into a profitable bootstrapped business - without ever "
        "fabricating a number or bypassing the human approval queue"
    ),
    backstory=(
        "Data-driven SaaS CEO running a strict Build-Measure-Learn loop. Has no "
        "access to payment methods; any action that costs money, creates a "
        "legal obligation, or becomes publicly visible must go through "
        "request_approval first. Computes scores only via evaluate_hypothesis "
        "(never by mental arithmetic), and escalates to the board via "
        "request_approval if check_escalation signals a fundamental strategy "
        "problem."
    ),
    llm=claude_llm,
    tools=[
        read_state, read_hypotheses, write_hypothesis, evaluate_hypothesis,
        check_escalation, compare_channel_performance, request_approval,
        read_channels, write_channel,
    ],
    verbose=True,
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
        "1) Call read_channels() to see the current roster. If it's empty, "
        "brainstorm a first set of candidate channels for this niche "
        "(Freqtrade/CCXT quant-bot users) and write each one with "
        "write_channel: id, name, a category (e.g. community_marketing, "
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
        "compare_channel_performance()."
    ),
    agent=ceo_agent,
    expected_output=(
        "Roster summary: each channel's status, impact/confidence, and real "
        "average score where evaluated. Any promotions or swaps made this "
        "cycle with their reasons. The final list of channels now "
        "status='testing' (<=3) for this cycle's hypothesis work to pick from."
    ),
)

task_growth = Task(
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
)

task_ceo = Task(
    description=(
        "Run the Build-Measure-Learn loop: "
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
        "returns escalate=true, file a request_approval (category 'pricing' "
        "or 'publish', whichever fits) explicitly flagging 'fundamental "
        "strategy change needed' instead of quietly pivoting again. "
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
)

task_dev = Task(
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
)

# Crew instanziieren
crew = Crew(
    agents=[growth_agent, ceo_agent, dev_agent],
    tasks=[task_channel_strategy, task_growth, task_ceo, task_dev],
    process=Process.sequential,
)

if __name__ == "__main__":
    print("[api-sentinel] Autonomous Loop Started (Anthropic Claude)...")
    crew.kickoff()
    print("[api-sentinel] Execution finished.")
