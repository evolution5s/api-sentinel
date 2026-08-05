"""Standalone checkup suite for api-sentinel. Exercises every tool and
scoring function against a disposable STATE_DIR.

Deliberately does NOT call crew.kickoff() (that would spend real Anthropic
API credits) and does NOT call open_pull_request with a real token (none is
configured here, and even if one were, a test run must never create a real
GitHub branch/PR as a side effect).

Run: python checkup.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="api_sentinel_checkup_"))
os.environ["STATE_DIR"] = str(SCRATCH_DIR)

import scoring  # noqa: E402
import tools  # noqa: E402
import approve  # noqa: E402
import crew  # noqa: E402

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
    except AssertionError as exc:
        results.append((name, "FAIL", str(exc)))
    except Exception as exc:
        results.append((name, "ERROR", f"{type(exc).__name__}: {exc}"))


def reset_state():
    if SCRATCH_DIR.exists():
        shutil.rmtree(SCRATCH_DIR)
    SCRATCH_DIR.mkdir(parents=True)
    _seed_testing_channel()


def _seed_testing_channel(channel_id="reddit"):
    """Most hypothesis tests need a channel that's already status='testing'
    (write_hypothesis now enforces this). Called on every reset_state() so
    existing tests don't each need their own roster setup; tests that care
    about a different channel id seed their own on top of this.
    """
    tools.write_channel.run(channel=json.dumps({
        "id": channel_id, "name": channel_id.title(), "category": "community_marketing",
        "is_paid": False, "impact_score": 3, "confidence_score": 3, "status": "testing",
    }))


SAMPLE_HYP = {
    "id": "hyp_test_0001",
    "statement": "test statement",
    "category": "revenue",
    "landing_page_variant_id": "lp_v1_default",
    "failure_rate": 0.001,
    "success_rate": 0.01,
    "duration_days": 10,
    "channel": "reddit",
}


# --- scoring.py: score formula ------------------------------------------

def test_score_at_failure_rate():
    reset_state()
    score = scoring.compute_score(conversions=1, estimated_reach=1000, failure_rate=0.001, success_rate=0.01)
    assert score == -1.0, f"expected -1.0, got {score}"


def test_score_at_success_rate():
    score = scoring.compute_score(conversions=10, estimated_reach=1000, failure_rate=0.001, success_rate=0.01)
    assert score == 1.0, f"expected 1.0, got {score}"


def test_score_at_midpoint():
    score = scoring.compute_score(conversions=5.5, estimated_reach=1000, failure_rate=0.001, success_rate=0.01)
    assert score == 0.0, f"expected 0.0, got {score}"


def test_score_clamped_below_failure():
    score = scoring.compute_score(conversions=0, estimated_reach=1000, failure_rate=0.001, success_rate=0.01)
    assert score == -1.0, f"expected clamped -1.0, got {score}"


def test_score_clamped_above_success():
    score = scoring.compute_score(conversions=100, estimated_reach=1000, failure_rate=0.001, success_rate=0.01)
    assert score == 1.0, f"expected clamped 1.0, got {score}"


def test_score_rejects_zero_reach():
    try:
        scoring.compute_score(1, 0, 0.001, 0.01)
        assert False, "expected ValueError for zero reach"
    except ValueError:
        pass


def test_score_rejects_equal_rates():
    try:
        scoring.compute_score(1, 1000, 0.01, 0.01)
        assert False, "expected ValueError for equal failure/success rate"
    except ValueError:
        pass


def test_verdict_bands():
    cases = [
        (1.0, "strongly validated"), (0.7, "strongly validated"),
        (0.6, "weakly positive"), (0.3, "weakly positive"),
        (0.2, "inconclusive"), (-0.3, "inconclusive"),
        (-0.4, "weakly negative"), (-0.7, "weakly negative"),
        (-0.8, "strongly devalidated"), (-1.0, "strongly devalidated"),
    ]
    for score, expected in cases:
        got = scoring.verdict_for_score(score)
        assert got == expected, f"score {score}: expected {expected}, got {got}"


# --- scoring.py: reach estimation ----------------------------------------

def test_estimate_reach_reddit_real():
    reach, source = scoring.estimate_reach("reddit", {"views": 500})
    assert (reach, source) == (500.0, "real")


def test_estimate_reach_reddit_upvotes():
    reach, source = scoring.estimate_reach("reddit", {"upvotes": 10})
    assert reach == 1000.0 and source == "estimated_upvotes", (reach, source)


def test_estimate_reach_reddit_comments_fallback():
    reach, source = scoring.estimate_reach("reddit", {"comments": 5})
    assert reach == 100.0 and source == "estimated_comments_low_confidence", (reach, source)


def test_estimate_reach_reddit_no_data():
    try:
        scoring.estimate_reach("reddit", {})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_estimate_reach_x_impressions():
    reach, source = scoring.estimate_reach("x", {"impressions": 5000})
    assert (reach, source) == (5000.0, "real")


def test_estimate_reach_x_engagement():
    reach, source = scoring.estimate_reach("x", {"likes": 10, "retweets": 5, "replies": 2, "bookmarks": 3})
    assert reach == 20 * 200 and source == "estimated_engagement", (reach, source)


def test_estimate_reach_discord():
    reach, source = scoring.estimate_reach("discord", {"members": 200})
    assert reach == 20.0 and source == "estimated_members", (reach, source)


def test_estimate_reach_landing_page():
    try:
        scoring.estimate_reach("landing_page_direct", {})
        assert False, "expected ValueError without real visits"
    except ValueError:
        pass
    reach, source = scoring.estimate_reach("landing_page_direct", {"visits": 42})
    assert (reach, source) == (42.0, "real")


def test_estimate_reach_unknown_channel():
    try:
        scoring.estimate_reach("carrier_pigeon", {"members": 1})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_update_reach_multiplier_roundtrip():
    original_file = scoring.REACH_ESTIMATORS_FILE
    scratch_file = SCRATCH_DIR / "reach_estimators_test.json"
    shutil.copy(original_file, scratch_file)
    scoring.REACH_ESTIMATORS_FILE = scratch_file
    try:
        before = scoring.load_reach_estimators()["reddit"]["upvote_to_view_multiplier"]
        updated = scoring.update_reach_multiplier("reddit", "upvote_to_view_multiplier", 150, "test recalibration")
        assert updated["reddit"]["upvote_to_view_multiplier"] == 150
        assert updated["history"][-1]["old_value"] == before
        assert updated["history"][-1]["new_value"] == 150
        reloaded = scoring.load_reach_estimators()
        assert reloaded["reddit"]["upvote_to_view_multiplier"] == 150
    finally:
        scoring.REACH_ESTIMATORS_FILE = original_file


# --- tools.py: JSONL primitives -------------------------------------------

def test_jsonl_roundtrip():
    reset_state()
    tools._append_jsonl("scratch.jsonl", {"a": 1})
    tools._append_jsonl("scratch.jsonl", {"a": 2})
    assert tools._read_jsonl("scratch.jsonl") == [{"a": 1}, {"a": 2}]


def test_jsonl_read_missing_file_returns_empty():
    reset_state()
    assert tools._read_jsonl("does_not_exist.jsonl") == []


def test_jsonl_write_overwrites():
    reset_state()
    tools._write_jsonl("scratch2.jsonl", [{"x": 1}, {"x": 2}])
    tools._write_jsonl("scratch2.jsonl", [{"x": 3}])
    assert tools._read_jsonl("scratch2.jsonl") == [{"x": 3}]


# --- tools.py: request_approval / read_state -------------------------------

def test_request_approval_valid():
    reset_state()
    result = json.loads(tools.request_approval.run(category="publish", proposal="p", reasoning="r"))
    assert "queued" in result
    stored = tools._read_jsonl("approval_queue.jsonl")
    assert len(stored) == 1 and stored[0]["status"] == "pending" and stored[0]["category"] == "publish"


def test_request_approval_invalid_category():
    reset_state()
    result = json.loads(tools.request_approval.run(category="marketing", proposal="p", reasoning="r"))
    assert "error" in result
    assert tools._read_jsonl("approval_queue.jsonl") == []


def test_read_state_reports_pending_approvals():
    reset_state()
    tools.request_approval.run(category="spend", proposal="p", reasoning="r")
    state = json.loads(tools.read_state.run())
    assert state["pending_approvals"] == 1
    assert state["total_approval_requests"] == 1
    assert state["signup_source"].startswith("github_issues")


# --- tools.py: hypotheses -------------------------------------------------

def test_write_hypothesis_create_requires_fields():
    reset_state()
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({"id": "hyp_x"})))
    assert "error" in result


def test_write_hypothesis_create_and_read():
    reset_state()
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP)))
    assert result.get("ok") is True
    all_hyps = json.loads(tools.read_hypotheses.run())
    assert len(all_hyps) == 1
    stored = all_hyps[0]
    assert stored["status"] == "active"
    assert stored["measured"] == {"conversions": 0, "reach_estimate": None, "reach_source": None}


def test_write_hypothesis_update_merges():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    patch = {"id": "hyp_test_0001", "measured": {"reach_estimate": 5000, "reach_source": "estimated_upvotes"}}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(patch)))
    assert result.get("ok") is True
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["measured"]["reach_estimate"] == 5000
    assert stored["measured"]["conversions"] == 0


def test_write_hypothesis_parallelism_limit():
    reset_state()
    for i in range(2):
        h = dict(SAMPLE_HYP)
        h["id"] = f"hyp_parallel_{i}"
        result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(h)))
        assert result.get("ok") is True, result
    third = dict(SAMPLE_HYP)
    third["id"] = "hyp_parallel_2"
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(third)))
    assert "error" in result, "expected the 3rd active hypothesis on the same variant to be rejected"


def test_read_hypotheses_status_filter():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    assert len(json.loads(tools.read_hypotheses.run(status="active"))) == 1
    assert len(json.loads(tools.read_hypotheses.run(status="evaluated"))) == 0


def test_evaluate_hypothesis_without_reach_estimate_errors():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.evaluate_hypothesis.run(hypothesis_id="hyp_test_0001"))
    assert "error" in result


def test_evaluate_hypothesis_counts_matching_signups():
    reset_state()
    created_at = datetime.now(timezone.utc) - timedelta(days=1)
    hyp = dict(SAMPLE_HYP)
    hyp["created_at"] = created_at.isoformat()
    tools.write_hypothesis.run(hypothesis=json.dumps(hyp))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001",
        "measured": {"reach_estimate": 1000, "reach_source": "estimated_upvotes"},
    }))

    inside_ts = (created_at + timedelta(hours=2)).isoformat()
    for i in range(3):
        tools._append_jsonl("signups.jsonl", {
            "issue_number": i, "landing_page_variant_id": "lp_v1_default", "submitted_at": inside_ts,
        })
    tools._append_jsonl("signups.jsonl", {
        "issue_number": 99, "landing_page_variant_id": "lp_v2_other", "submitted_at": inside_ts,
    })
    outside_ts = (created_at - timedelta(days=5)).isoformat()
    tools._append_jsonl("signups.jsonl", {
        "issue_number": 100, "landing_page_variant_id": "lp_v1_default", "submitted_at": outside_ts,
    })

    result = json.loads(tools.evaluate_hypothesis.run(hypothesis_id="hyp_test_0001"))
    assert result["conversions"] == 3, result
    assert result["estimated_reach"] == 1000
    expected_score = scoring.compute_score(3, 1000, 0.001, 0.01)
    assert result["score"] == expected_score
    assert result["verdict"] == scoring.verdict_for_score(expected_score)


def test_check_escalation_triggers_on_low_rolling_average():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps({**SAMPLE_HYP, "id": "hyp_a", "score": -0.8, "status": "evaluated"}))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_b", "landing_page_variant_id": "lp_v2_x",
        "score": -0.6, "status": "evaluated", "prior_hypothesis_id": "hyp_a",
    }))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_c", "landing_page_variant_id": "lp_v3_x",
        "score": -0.5, "status": "evaluated", "prior_hypothesis_id": "hyp_b",
    }))
    result = json.loads(tools.check_escalation.run(hypothesis_id="hyp_c"))
    assert result["escalate"] is True, result
    assert result["rolling_average"] == round((-0.8 - 0.6 - 0.5) / 3, 3)


def test_check_escalation_no_scores_yet():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.check_escalation.run(hypothesis_id="hyp_test_0001"))
    assert result["escalate"] is False


def test_write_hypothesis_rejects_channel_not_testing():
    reset_state()
    tools.write_channel.run(channel=json.dumps({"id": "reddit", "status": "bench"}), reason="testing rediscovered stale")
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP)))
    assert "error" in result and "not currently status='testing'" in result["error"]


def test_write_hypothesis_rejects_unknown_channel():
    reset_state()
    bad = {**SAMPLE_HYP, "channel": "carrier_pigeon"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result


# --- tools.py: channel roster (Bullseye framework) --------------------------

def test_write_channel_create_requires_fields():
    reset_state()
    result = json.loads(tools.write_channel.run(channel=json.dumps({"id": "seo"})))
    assert "error" in result


def test_write_channel_create_defaults_not_tested():
    reset_state()
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "content_marketing", "name": "Content marketing", "category": "content_marketing",
        "is_paid": False, "impact_score": 4, "confidence_score": 2,
    })))
    assert result == {"ok": True, "id": "content_marketing", "status": "not_tested"}
    stored = json.loads(tools.read_channels.run())
    entry = next(c for c in stored if c["id"] == "content_marketing")
    assert entry["status_history"][0]["to"] == "not_tested"


def test_write_channel_testing_cap_enforced():
    reset_state()  # "reddit" already testing from reset_state
    _seed_testing_channel("discord")
    _seed_testing_channel("telegram")
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "seo", "name": "SEO", "category": "seo", "is_paid": False,
        "impact_score": 2, "confidence_score": 2, "status": "testing",
    })))
    assert "error" in result and "testing cap reached" in result["error"]


def test_write_channel_status_change_requires_reason():
    reset_state()
    result = json.loads(tools.write_channel.run(channel=json.dumps({"id": "reddit", "status": "bench"})))
    assert "error" in result and "requires a non-empty reason" in result["error"]


def test_write_channel_status_change_with_reason_logs_history():
    reset_state()
    result = json.loads(tools.write_channel.run(
        channel=json.dumps({"id": "reddit", "status": "bench"}), reason="scored -0.5 over 3 tests",
    ))
    assert result == {"ok": True, "id": "reddit", "status": "bench"}
    entry = json.loads(tools.read_channels.run())[0]
    assert entry["status_history"][-1] == {"at": entry["updated_at"], "from": "testing", "to": "bench", "reason": "scored -0.5 over 3 tests"}


def test_write_channel_paid_requires_approved_spend_request():
    reset_state()
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
    })))
    assert "error" in result and "approved_request_id" in result["error"]


def test_write_channel_paid_succeeds_with_approved_spend_request():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="spend", proposal="$1500 reddit ads test", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
        "approved_request_id": appr["queued"],
    })))
    assert result == {"ok": True, "id": "reddit_ads", "status": "testing"}


def test_write_channel_paid_rejects_pending_approval():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="spend", proposal="$1500 reddit ads test", reasoning="r"))
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
        "approved_request_id": appr["queued"],
    })))
    assert "error" in result


def test_read_channels_status_filter():
    reset_state()
    _seed_testing_channel("discord")
    tools.write_channel.run(channel=json.dumps({
        "id": "seo", "name": "SEO", "category": "seo", "is_paid": False,
        "impact_score": 2, "confidence_score": 2,
    }))
    assert {c["id"] for c in json.loads(tools.read_channels.run(status="testing"))} == {"reddit", "discord"}
    assert {c["id"] for c in json.loads(tools.read_channels.run(status="not_tested"))} == {"seo"}


# --- tools.py: growth --------------------------------------------------------

def test_read_channel_metrics_tool_reddit():
    reset_state()
    result = json.loads(tools.read_channel_metrics.run(channel="reddit", metrics_json=json.dumps({"upvotes": 20})))
    assert result["estimated_reach"] == 2000.0 and result["reach_source"] == "estimated_upvotes"


def test_read_channel_metrics_tool_bad_json():
    reset_state()
    result = json.loads(tools.read_channel_metrics.run(channel="reddit", metrics_json="not json"))
    assert "error" in result


def test_read_channel_metrics_tool_no_data():
    reset_state()
    result = json.loads(tools.read_channel_metrics.run(channel="x", metrics_json="{}"))
    assert "error" in result


def test_read_channel_metrics_tool_x_source_url_explains_no_free_api():
    reset_state()
    result = json.loads(tools.read_channel_metrics.run(channel="x", source_url="https://x.com/someone/status/123"))
    assert "error" in result
    assert "fetch_note" in result and "no free public metrics API" in result["fetch_note"]


def test_fetch_reddit_public_metrics_bad_url_raises():
    try:
        tools.fetch_reddit_public_metrics("https://www.reddit.com/r/this_subreddit_should_not_exist_xyz_abc_123/comments/000000/x/")
        assert False, "expected an exception for a nonexistent post"
    except Exception:
        pass


def _discover_live_reddit_post_url():
    """Reddit blocks a meaningful share of cloud/datacenter IPs with a 403
    regardless of User-Agent - including, as observed running this suite,
    this very sandbox. Raises requests.HTTPError in that case; callers
    decide how to treat that (see the two tests below).
    """
    listing = requests.get(
        "https://www.reddit.com/r/announcements/top.json?limit=1&t=all",
        headers={"User-Agent": tools.REDDIT_USER_AGENT}, timeout=15,
    )
    listing.raise_for_status()
    post = listing.json()["data"]["children"][0]["data"]
    return "https://www.reddit.com" + post["permalink"]


def test_fetch_reddit_public_metrics_live():
    try:
        post_url = _discover_live_reddit_post_url()
    except requests.HTTPError as exc:
        print(f"    (skipped live assertion - Reddit blocked this environment: {exc})")
        return
    metrics = tools.fetch_reddit_public_metrics(post_url)
    assert isinstance(metrics["upvotes"], int) and metrics["upvotes"] >= 0
    assert isinstance(metrics["comments"], int) and metrics["comments"] >= 0


def test_read_channel_metrics_tool_reddit_source_url_live():
    reset_state()
    try:
        post_url = _discover_live_reddit_post_url()
    except requests.HTTPError as exc:
        print(f"    (skipped live assertion - Reddit blocked this environment: {exc})")
        return
    result = json.loads(tools.read_channel_metrics.run(channel="reddit", source_url=post_url))
    assert "estimated_reach" in result, result
    assert result["fetch_note"] == "auto-fetched from Reddit's public JSON endpoint"


def test_fetch_discord_public_metrics_bad_invite_raises():
    try:
        tools.fetch_discord_public_metrics("this-invite-code-should-not-exist-xyz-123")
        assert False, "expected an exception for a nonexistent invite"
    except Exception:
        pass


def test_read_channel_metrics_tool_discord_source_url():
    reset_state()
    # Discord's own community server invite - long-lived, used as a stable
    # smoke-test target. Network-dependent: must degrade gracefully either way.
    result = json.loads(tools.read_channel_metrics.run(channel="discord", source_url="https://discord.gg/discord"))
    assert "estimated_reach" in result or "error" in result, result


def test_fetch_telegram_public_metrics_bad_channel_raises():
    try:
        tools.fetch_telegram_public_metrics("this_channel_should_not_exist_xyz_123_abc")
        assert False, "expected an exception for a nonexistent channel"
    except Exception:
        pass


def test_read_channel_metrics_tool_telegram_source_url():
    reset_state()
    # Telegram's own official announcement channel - stable smoke-test target.
    result = json.loads(tools.read_channel_metrics.run(channel="telegram", source_url="telegram"))
    assert "estimated_reach" in result or "error" in result, result


# --- tools.py: compare_channel_performance ----------------------------------

def test_compare_channel_performance_empty():
    reset_state()  # seeds one roster channel ("reddit"), no evaluated hypotheses yet
    result = json.loads(tools.compare_channel_performance.run())
    assert result["ranked"] == []
    assert result["untested_channels"] == ["reddit"]


def test_compare_channel_performance_ranks_by_score():
    reset_state()
    _seed_testing_channel("x")
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_reddit_1", "channel": "reddit", "status": "evaluated", "score": 0.8,
    }))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_reddit_2", "landing_page_variant_id": "lp_v2",
        "channel": "reddit", "status": "evaluated", "score": 0.4,
    }))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_x_1", "landing_page_variant_id": "lp_v3",
        "channel": "x", "status": "evaluated", "score": -0.6,
    }))
    result = json.loads(tools.compare_channel_performance.run())
    assert result["ranked"][0] == {"channel": "reddit", "average_score": 0.6, "evaluated_hypotheses": 2}
    assert result["ranked"][1] == {"channel": "x", "average_score": -0.6, "evaluated_hypotheses": 1}
    assert result["untested_channels"] == []


# --- tools.py: dev -----------------------------------------------------------

def test_open_pull_request_requires_token():
    reset_state()
    had_token = os.environ.pop("GITHUB_TOKEN", None)
    try:
        result = json.loads(tools.open_pull_request.run(
            branch_name="test", file_path="test.html", file_content="<html></html>",
            pr_title="t", pr_body="b",
        ))
        assert "error" in result and "GITHUB_TOKEN" in result["error"]
    finally:
        if had_token is not None:
            os.environ["GITHUB_TOKEN"] = had_token


# --- tools.py: cycle notification -------------------------------------------

def test_send_telegram_message_skips_without_credentials():
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        tools.send_telegram_message("test")  # must not raise
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_send_telegram_message_degrades_gracefully_on_bad_token():
    os.environ["TELEGRAM_BOT_TOKEN"] = "invalid-token-for-checkup"
    os.environ["TELEGRAM_CHAT_ID"] = "0"
    try:
        tools.send_telegram_message("test")  # real network call, bad token -> must not raise
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)


def test_sync_signups_from_github_via_read_state():
    reset_state()
    state = json.loads(tools.read_state.run())
    assert state["signup_source"].startswith("github_issues")


# --- approve.py --------------------------------------------------------------

def test_approve_cli_flow():
    reset_state()
    tools.request_approval.run(category="publish", proposal="p1", reasoning="r1")
    records = approve._load()
    assert len(records) == 1
    request_id = records[0]["id"]

    records = approve.decide(records, request_id, "approved", "looks fine")
    approve._save(records)
    reloaded = approve._load()
    assert reloaded[0]["status"] == "approved"
    assert reloaded[0]["decision_reason"] == "looks fine"

    records2 = approve._load()
    records2 = approve.decide(records2, request_id, "rejected")
    approve._save(records2)
    final = approve._load()
    assert final[0]["status"] == "approved", "an already-decided request must not be overwritten"


# --- crew.py: construction sanity (no kickoff, no API calls) ---------------

def test_crew_has_three_agents_and_four_tasks():
    assert len(crew.crew.agents) == 3
    assert len(crew.crew.tasks) == 4


def test_ceo_agent_tools_match_spec():
    tool_names = {t.name for t in crew.ceo_agent.tools}
    assert tool_names == {
        "read_state", "read_hypotheses", "write_hypothesis", "evaluate_hypothesis",
        "check_escalation", "compare_channel_performance", "request_approval",
        "read_channels", "write_channel",
    }, tool_names


def test_growth_dev_tools():
    assert {t.name for t in crew.growth_agent.tools} == {"request_approval", "read_channel_metrics", "read_channels"}
    assert {t.name for t in crew.dev_agent.tools} == {"open_pull_request"}


def test_channel_strategy_task_assigned_to_ceo():
    assert crew.task_channel_strategy.agent is crew.ceo_agent
    assert crew.crew.tasks[0] is crew.task_channel_strategy


def test_send_cycle_summary_never_raises_without_a_crew_run():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        crew.send_cycle_summary()  # no kickoff() happened; task.output is None on every task
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token


def main():
    tests = sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        check(name, fn)

    print("=" * 70)
    print("api-sentinel checkup report")
    print("=" * 70)
    for name, status, detail in results:
        marker = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR "}[status]
        line = f"[{marker}] {name}"
        if detail:
            line += f" -- {detail}"
        print(line)

    passed = sum(1 for _, status, _ in results if status == "PASS")
    failed = [r for r in results if r[1] != "PASS"]
    print("-" * 70)
    print(f"{passed}/{len(results)} passed")
    if failed:
        print(f"{len(failed)} FAILED/ERRORED:")
        for name, status, detail in failed:
            print(f"  - {name}: {status} {detail}")

    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
