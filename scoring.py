"""Pure, testable functions for the Hypothesis Engine: the score formula and
reach estimation (brief section 5.3). Deliberately has no CrewAI or STATE_DIR
dependency so the arithmetic can be unit-tested in isolation from the agent
runtime and from disk state.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

REACH_ESTIMATORS_FILE = Path(__file__).parent / "reach_estimators.json"


def load_reach_estimators() -> dict:
    with REACH_ESTIMATORS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_score(conversions: float, estimated_reach: float, failure_rate: float, success_rate: float) -> float:
    """Section 5.3 score formula, continuous -1..1 in 0.1 steps.

    rate  = conversions / estimated_reach
    score = clamp(round((2*(rate-failure_rate)/(success_rate-failure_rate) - 1) / 0.1) * 0.1, -1, 1)
    """
    if estimated_reach <= 0:
        raise ValueError("estimated_reach must be > 0 to compute a rate")
    if success_rate == failure_rate:
        raise ValueError("success_rate and failure_rate must differ")

    rate = conversions / estimated_reach
    raw_steps = (2 * (rate - failure_rate) / (success_rate - failure_rate) - 1) / 0.1
    stepped = round(raw_steps) * 0.1
    return clamp(round(stepped, 1), -1.0, 1.0)


def verdict_for_score(score: float) -> str:
    """Section 5.4 decision bands. Human-readable narrative label only -
    the four-way outcome workflow branches on classify_outcome() below, not
    on this string, but the band boundaries (0.7/0.3/-0.3/-0.7) are shared
    between the two so they never quietly disagree.
    """
    if score >= 0.7:
        return "strongly validated"
    if score >= 0.3:
        return "weakly positive"
    if score >= -0.3:
        return "inconclusive"
    if score >= -0.7:
        return "weakly negative"
    return "strongly devalidated"


PIVOT_ATTEMPT_CAP = 2
HYPOTHESIS_OUTCOMES = {"build", "test_further", "pivot", "bury"}


def compute_break_even_users(build_cost: float, price_point_monthly: float, horizon_months: float) -> int:
    """How many paying users, sustained for horizon_months at
    price_point_monthly, are needed to recoup build_cost. Ceiling - a
    fractional user isn't a real break-even point. This is the number that
    turns a "test further" into a "build" for one specific hypothesis;
    there is no global user-count target (see classify_outcome).
    """
    if build_cost <= 0:
        raise ValueError("build_cost must be > 0")
    if price_point_monthly <= 0:
        raise ValueError("price_point_monthly must be > 0")
    if horizon_months <= 0:
        raise ValueError("horizon_months must be > 0")
    return math.ceil(build_cost / (price_point_monthly * horizon_months))


def classify_outcome(
    score: float,
    conversions: float,
    break_even_users: int,
    extension_used: bool,
    pivot_attempts_so_far: int,
) -> str:
    """Deterministic four-way outcome bucket (bury/pivot/test_further/build)
    for one evaluated hypothesis. Deliberately not agent judgment - both the
    "test further" and "pivot" paths could otherwise loop indefinitely, so
    the caps here (one extension, PIVOT_ATTEMPT_CAP pivots) are enforced in
    code, not left to a model deciding case by case.

    - Build requires BOTH a strong rate (score >= 0.7) AND enough real
      conversions to actually clear this hypothesis's own break-even bar -
      a great rate on too few real users is "test further", not "build".
      This is what makes a tiny sample a legitimate Build basis when the
      break-even bar is genuinely low, and stops a small positive sample
      from being read as validating a hypothesis with a high break-even bar.
    - Test further fires once (guarded by extension_used, the same flag the
      existing single-extension mechanic already uses) for any score that
      isn't already clearly negative, whether that's an ambiguous rate or a
      strong rate without enough real conversions yet.
    - Below that: a clearly bad score (< -0.7), or an ambiguous score that
      already used its one extension and still didn't clear into Build, is
      Bury once the pivot budget is spent, Pivot otherwise.
    """
    if score >= 0.7 and conversions >= break_even_users:
        return "build"

    if not extension_used and score >= -0.3:
        return "test_further"

    if score < -0.7:
        return "bury"

    return "pivot" if pivot_attempts_so_far < PIVOT_ATTEMPT_CAP else "bury"


def estimate_reach(channel: str, metrics: dict, estimators: dict = None) -> tuple:
    """Returns (estimated_reach, reach_source). Always prefers a directly
    supplied real/native metric (views/impressions/visits) over the fallback
    formula. Raises ValueError instead of guessing if nothing usable was
    supplied for the given channel - reach must never be fabricated.
    """
    estimators = estimators or load_reach_estimators()

    if channel == "reddit":
        if metrics.get("views") is not None:
            return float(metrics["views"]), "real"
        if metrics.get("upvotes") is not None:
            mult = estimators["reddit"]["upvote_to_view_multiplier"]
            return float(metrics["upvotes"]) * mult, "estimated_upvotes"
        if metrics.get("comments") is not None:
            mult = estimators["reddit"]["comment_to_view_multiplier"]
            return float(metrics["comments"]) * mult, "estimated_comments_low_confidence"
        raise ValueError("reddit: need one of views, upvotes, comments")

    if channel == "x":
        if metrics.get("impressions") is not None:
            return float(metrics["impressions"]), "real"
        engagement_keys = ("likes", "retweets", "replies", "bookmarks")
        if any(metrics.get(k) is not None for k in engagement_keys):
            engagement = sum(metrics.get(k) or 0 for k in engagement_keys)
            mult = estimators["x"]["engagement_to_impression_multiplier"]
            return float(engagement) * mult, "estimated_engagement"
        raise ValueError("x: need impressions, or at least one of likes/retweets/replies/bookmarks")

    if channel in ("discord", "telegram"):
        if metrics.get("members") is None:
            raise ValueError(f"{channel}: need members")
        mult = estimators["discord_telegram"]["member_to_view_multiplier"]
        return float(metrics["members"]) * mult, "estimated_members"

    if channel == "landing_page_direct":
        if metrics.get("visits") is None:
            raise ValueError("landing_page_direct: need visits from real analytics - no fallback formula exists for this channel")
        return float(metrics["visits"]), "real"

    raise ValueError(f"unknown channel '{channel}'")


def update_reach_multiplier(channel_key: str, multiplier_key: str, new_value: float, reason: str) -> dict:
    """Recalibrate a fallback multiplier once enough real data points exist
    for a channel, logging date and old/new value so the change stays
    traceable (section 5.3). Nothing calls this automatically today - judging
    when "enough" data points exist is left to the CEO agent's discretion.
    """
    estimators = load_reach_estimators()
    old_value = estimators[channel_key][multiplier_key]
    estimators[channel_key][multiplier_key] = new_value
    estimators.setdefault("history", []).append({
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel_key,
        "multiplier": multiplier_key,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    })
    with REACH_ESTIMATORS_FILE.open("w", encoding="utf-8") as f:
        json.dump(estimators, f, ensure_ascii=False, indent=2)
    return estimators
