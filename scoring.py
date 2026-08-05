"""Pure, testable functions for the Hypothesis Engine: the score formula and
reach estimation (brief section 5.3). Deliberately has no CrewAI or STATE_DIR
dependency so the arithmetic can be unit-tested in isolation from the agent
runtime and from disk state.
"""
import json
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
    """Section 5.4 decision bands."""
    if score >= 0.7:
        return "strongly validated"
    if score >= 0.3:
        return "weakly positive"
    if score >= -0.3:
        return "inconclusive"
    if score >= -0.7:
        return "weakly negative"
    return "strongly devalidated"


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
