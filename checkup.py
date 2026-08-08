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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="api_sentinel_checkup_"))
os.environ["STATE_DIR"] = str(SCRATCH_DIR)

import scoring  # noqa: E402
import tools  # noqa: E402
import holding  # noqa: E402
import approve  # noqa: E402
import crew  # noqa: E402
import pricing  # noqa: E402

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


def _allow_paid_channels():
    """Most existing paid-channel tests predate the paid_channels_allowed
    policy gate and want to test the approval-queue mechanics specifically,
    not the policy gate - this flips the policy on first via the real
    update_subsidiary_policies flow (approval-gated, same as production).
    """
    holding.read_subsidiaries.run()  # bootstraps api-sentinel
    appr = json.loads(tools.request_approval.run(category="pricing", proposal="allow paid channels", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)
    holding.update_subsidiary_policies.run(
        subsidiary_id="api-sentinel", policies_patch=json.dumps({"paid_channels_allowed": True}),
        approved_request_id=appr["queued"], reasoning="test setup",
    )


SAMPLE_HYP = {
    "id": "hyp_test_0001",
    "statement": "test statement",
    "category": "revenue",
    "landing_page_variant_id": "lp_v1_default",
    "failure_rate": 0.001,
    "success_rate": 0.01,
    "duration_days": 10,
    "channel": "reddit",
    "hypothesis_type": "value",
    "estimated_build_cost": 1000,
    "price_point_monthly": 20,
    "break_even_horizon_months": 6,
    "break_even_users": 9,  # ceil(1000 / (20 * 6))
    "impact_score": 3,
    "confidence_score": 3,
    "primary_variable_tested": "audience",  # required for a first attempt (no prior_hypothesis_id)
}

SAMPLE_PIVOT = {
    "nature_of_change": "Shift from Freqtrade/CCXT niche to general crypto trading tools",
    "validating_data": "3-hypothesis rolling average of -0.6",
    "evolutionary_or_disruptive": "disruptive",
    "existing_business_disposition": "pause organic channels, keep landing page live",
    "capability_gap_analysis": "no new agents needed, same tool set applies",
    "new_resources_needed": "none identified yet",
    "risk_assessment": "moderate - broader audience but diluted positioning",
    "synergy_overlap": "none with other subsidiaries (only one exists)",
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


# --- scoring.py: break-even / four-way outcome ----------------------------

def test_compute_break_even_users():
    assert scoring.compute_break_even_users(1000, 20, 6) == 9  # ceil(1000/120)
    assert scoring.compute_break_even_users(40, 20, 1) == 2  # exact division


def test_compute_break_even_users_rejects_non_positive_inputs():
    for kwargs in (
        dict(build_cost=0, price_point_monthly=20, horizon_months=6),
        dict(build_cost=100, price_point_monthly=0, horizon_months=6),
        dict(build_cost=100, price_point_monthly=20, horizon_months=0),
    ):
        try:
            scoring.compute_break_even_users(**kwargs)
            assert False, f"expected ValueError for {kwargs}"
        except ValueError:
            pass


def test_classify_outcome_build_requires_both_score_and_sample():
    # strong score + enough real conversions -> build
    assert scoring.classify_outcome(0.8, 5, 2, False, 0) == "build"
    # strong score but NOT enough real conversions relative to a high bar -> test_further
    assert scoring.classify_outcome(0.8, 1, 50, False, 0) == "test_further"


def test_classify_outcome_test_further_fires_once():
    assert scoring.classify_outcome(0.0, 3, 10, False, 0) == "test_further"
    # already extended once and still ambiguous -> forced decision, not a second test_further
    assert scoring.classify_outcome(0.0, 3, 10, True, 0) in ("pivot", "bury")


def test_classify_outcome_pivot_then_bury_once_cap_reached():
    assert scoring.classify_outcome(-0.5, 1, 10, False, 0) == "pivot"
    assert scoring.classify_outcome(-0.5, 1, 10, False, scoring.PIVOT_ATTEMPT_CAP) == "bury"


def test_classify_outcome_clearly_bad_score_is_always_bury():
    assert scoring.classify_outcome(-0.9, 0, 10, False, 0) == "bury"


# --- pricing.py: date-aware Anthropic cost lookup ---------------------------

def test_get_pricing_haiku():
    rates = pricing.get_pricing("claude-haiku-4-5", date(2026, 1, 1))
    assert rates == {
        "base_input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
        "cache_hit": 0.10, "output": 5.0,
    }


def test_get_pricing_sonnet5_before_price_step():
    rates = pricing.get_pricing("claude-sonnet-5", date(2026, 8, 31))
    assert rates == {
        "base_input": 2.0, "cache_write_5m": 2.50, "cache_write_1h": 4.0,
        "cache_hit": 0.20, "output": 10.0,
    }


def test_get_pricing_sonnet5_from_price_step():
    rates = pricing.get_pricing("claude-sonnet-5", date(2026, 9, 1))
    assert rates == {
        "base_input": 3.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0,
        "cache_hit": 0.30, "output": 15.0,
    }
    # a later date is still "from_step" - the step is one-directional
    assert pricing.get_pricing("claude-sonnet-5", date(2026, 12, 25)) == pricing.get_pricing(
        "claude-sonnet-5", date(2026, 9, 1)
    )


def test_get_pricing_unknown_model_raises():
    try:
        pricing.get_pricing("claude-opus-5", date(2026, 1, 1))
        assert False, "expected ValueError for an unpriced model"
    except ValueError:
        pass


def test_compute_cycle_cost_haiku_known_value():
    # 1M base input + 1M cache write + 1M cache hit + 1M output at
    # haiku-4-5's 5m rates = 1.00 + 1.25 + 0.10 + 5.00 = 7.35 USD exactly.
    cost = pricing.compute_cycle_cost(
        model="claude-haiku-4-5", as_of=date(2026, 1, 1),
        base_input_tokens=1_000_000, cache_write_tokens=1_000_000,
        cache_hit_tokens=1_000_000, completion_tokens=1_000_000,
    )
    assert cost == 7.35


def test_compute_cycle_cost_is_date_aware_for_sonnet5():
    kwargs = dict(
        model="claude-sonnet-5", base_input_tokens=1_000_000, cache_write_tokens=0,
        cache_hit_tokens=0, completion_tokens=0,
    )
    before = pricing.compute_cycle_cost(as_of=date(2026, 8, 31), **kwargs)
    after = pricing.compute_cycle_cost(as_of=date(2026, 9, 1), **kwargs)
    assert before == 2.0
    assert after == 3.0
    assert after > before


def test_compute_cycle_cost_1h_cache_write_tier():
    cost_5m = pricing.compute_cycle_cost(
        model="claude-haiku-4-5", as_of=date(2026, 1, 1),
        base_input_tokens=0, cache_write_tokens=1_000_000, cache_hit_tokens=0,
        completion_tokens=0, cache_write_ttl="5m",
    )
    cost_1h = pricing.compute_cycle_cost(
        model="claude-haiku-4-5", as_of=date(2026, 1, 1),
        base_input_tokens=0, cache_write_tokens=1_000_000, cache_hit_tokens=0,
        completion_tokens=0, cache_write_ttl="1h",
    )
    assert cost_5m == 1.25
    assert cost_1h == 2.0


def test_compute_cycle_cost_rejects_invalid_ttl():
    try:
        pricing.compute_cycle_cost(
            model="claude-haiku-4-5", as_of=date(2026, 1, 1),
            base_input_tokens=0, cache_write_tokens=0, cache_hit_tokens=0,
            completion_tokens=0, cache_write_ttl="30m",
        )
        assert False, "expected ValueError for an invalid cache_write_ttl"
    except ValueError:
        pass


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


def test_check_approval_status_unknown_id():
    reset_state()
    result = json.loads(tools.check_approval_status.run(approval_id="appr_doesnotexist"))
    assert "error" in result


def test_check_approval_status_reflects_real_state():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
    result = json.loads(tools.check_approval_status.run(approval_id=appr["queued"]))
    assert result == {"id": appr["queued"], "status": "pending", "category": "deploy"}

    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)
    result = json.loads(tools.check_approval_status.run(approval_id=appr["queued"]))
    assert result["status"] == "approved"


# --- tools.py: task orders (Sub-CEO -> Growth/Dev structured handoff) -------

def test_file_task_order_rejects_invalid_role():
    reset_state()
    result = json.loads(tools.file_task_order.run(
        to_role="marketing", task_description="do a thing", context="because",
    ))
    assert "error" in result


def test_file_and_read_task_order():
    reset_state()
    filed = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="build variant for hyp_x", context="build outcome",
        hypothesis_id="hyp_x",
    ))
    assert "filed" in filed
    open_orders = json.loads(tools.read_task_orders.run(to_role="dev", status="open"))
    assert len(open_orders) == 1
    assert open_orders[0]["hypothesis_id"] == "hyp_x"
    assert open_orders[0]["status"] == "open"
    # scoped by role - growth doesn't see dev's order
    assert json.loads(tools.read_task_orders.run(to_role="growth", status="open")) == []


def test_complete_task_order_roundtrip():
    reset_state()
    filed = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="measure reach", context="active hypothesis",
    ))
    order_id = filed["filed"]
    result = json.loads(tools.complete_task_order.run(order_id=order_id, result="reach=1200"))
    assert result.get("ok") is True

    open_orders = json.loads(tools.read_task_orders.run(to_role="growth", status="open"))
    assert open_orders == []
    all_orders = json.loads(tools.read_task_orders.run(to_role="growth"))
    assert all_orders[0]["status"] == "done" and all_orders[0]["result"] == "reach=1200"


def test_complete_task_order_does_not_overwrite_done():
    reset_state()
    filed = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="measure reach", context="active hypothesis",
    ))
    order_id = filed["filed"]
    tools.complete_task_order.run(order_id=order_id, result="first result")
    result = json.loads(tools.complete_task_order.run(order_id=order_id, result="second result"))
    assert "error" in result
    stored = json.loads(tools.read_task_orders.run(to_role="growth"))[0]
    assert stored["result"] == "first result"


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


def test_write_hypothesis_rejects_invalid_hypothesis_type():
    reset_state()
    bad = {**SAMPLE_HYP, "hypothesis_type": "vanity"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result and "hypothesis_type" in result["error"]


def test_write_hypothesis_rejects_invalid_status():
    reset_state()
    bad = {**SAMPLE_HYP, "status": "made_up"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result and "status" in result["error"]


def test_write_hypothesis_bury_requires_reasoning():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": "hyp_test_0001", "status": "buried"}),
    ))
    assert "error" in result and "bury_reasoning" in result["error"]

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "status": "buried", "outcome": "bury",
        "bury_reasoning": "score never cleared the threshold across 2 pivots",
    })))
    assert result.get("ok") is True
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["status"] == "buried"


def test_write_hypothesis_pivot_followup_requires_variable_and_reasoning():
    reset_state()
    _seed_testing_channel("reddit")
    tools.write_hypothesis.run(hypothesis=json.dumps({**SAMPLE_HYP, "outcome": "pivot"}))

    followup = {
        **SAMPLE_HYP, "id": "hyp_pivot_followup",
        "landing_page_variant_id": "lp_v2_pivot",
        "prior_hypothesis_id": "hyp_test_0001", "prior_score": -0.5,
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(followup)))
    assert "error" in result and "pivot_variable_changed" in result["error"]

    followup["pivot_variable_changed"] = "price"
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(followup)))
    assert "error" in result and "pivot_reasoning" in result["error"]

    followup["pivot_reasoning"] = "original price point was too high for this audience"
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(followup)))
    assert result.get("ok") is True


# --- tools.py: one-variable rule on first attempts too (four-fixes addendum, point 2) --

def test_write_hypothesis_first_attempt_requires_primary_variable_tested():
    reset_state()
    bad = dict(SAMPLE_HYP)
    del bad["primary_variable_tested"]
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result and "primary_variable_tested" in result["error"]


def test_write_hypothesis_first_attempt_rejects_invalid_primary_variable():
    reset_state()
    bad = {**SAMPLE_HYP, "primary_variable_tested": "everything_at_once"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result and "primary_variable_tested" in result["error"]


def test_write_hypothesis_first_attempt_succeeds_with_primary_variable():
    reset_state()
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP)))
    assert result.get("ok") is True
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["primary_variable_tested"] == "audience"
    assert stored["holding_constant_notes"] is None


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


# --- tools.py: MAX_ACTIVE_HYPOTHESES cap (four-fixes addendum, point 4) ----

def test_write_hypothesis_max_active_cap_enforced():
    reset_state()
    for i in range(tools.MAX_ACTIVE_HYPOTHESES):
        h = {**SAMPLE_HYP, "id": f"hyp_cap_{i}", "landing_page_variant_id": f"lp_v{i}_cap"}
        result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(h)))
        assert result.get("ok") is True, result
    overflow = {**SAMPLE_HYP, "id": "hyp_cap_overflow", "landing_page_variant_id": "lp_v_overflow"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(overflow)))
    assert "error" in result and "already status='active'" in result["error"], result


def test_write_hypothesis_max_active_cap_frees_up_after_evaluation():
    reset_state()
    for i in range(tools.MAX_ACTIVE_HYPOTHESES):
        h = {**SAMPLE_HYP, "id": f"hyp_cap_{i}", "landing_page_variant_id": f"lp_v{i}_cap"}
        tools.write_hypothesis.run(hypothesis=json.dumps(h))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_cap_0", "status": "evaluated", "outcome": "bury", "bury_reasoning": "weak signal",
    }))
    freed = {**SAMPLE_HYP, "id": "hyp_cap_new", "landing_page_variant_id": "lp_v_new"}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(freed)))
    assert result.get("ok") is True, result


def test_read_hypotheses_status_filter():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    assert len(json.loads(tools.read_hypotheses.run(status="active"))) == 1
    assert len(json.loads(tools.read_hypotheses.run(status="evaluated"))) == 0


# --- tools.py: mandatory time-box (four-fixes addendum, point 1) -----------

def test_read_due_hypotheses_not_due_before_duration_or_trigger():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))  # duration_days=10, created "now"
    assert json.loads(tools.read_due_hypotheses.run()) == []


def test_read_due_hypotheses_due_when_duration_elapsed():
    reset_state()
    created_at = datetime.now(timezone.utc) - timedelta(days=11)
    hyp = {**SAMPLE_HYP, "created_at": created_at.isoformat()}
    tools.write_hypothesis.run(hypothesis=json.dumps(hyp))
    due = json.loads(tools.read_due_hypotheses.run())
    assert len(due) == 1 and due[0]["id"] == SAMPLE_HYP["id"] and due[0]["reason"] == "duration_elapsed"


def test_read_due_hypotheses_due_early_via_sample_size_trigger():
    reset_state()
    hyp = {**SAMPLE_HYP, "sample_size_trigger": 500}
    tools.write_hypothesis.run(hypothesis=json.dumps(hyp))  # duration_days=10, not elapsed yet
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": SAMPLE_HYP["id"], "measured": {"reach_estimate": 600, "reach_source": "estimated_upvotes"},
    }))
    due = json.loads(tools.read_due_hypotheses.run())
    assert len(due) == 1 and due[0]["reason"] == "sample_size_trigger_met" and due[0]["reach_estimate"] == 600


def test_read_due_hypotheses_ignores_non_active():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "created_at": (datetime.now(timezone.utc) - timedelta(days=11)).isoformat(),
        "status": "buried", "bury_reasoning": "no longer relevant",
    }))
    assert json.loads(tools.read_due_hypotheses.run()) == []


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


def test_compute_break_even_tool():
    reset_state()
    result = json.loads(tools.compute_break_even.run(
        estimated_build_cost=1000, price_point_monthly=20, break_even_horizon_months=6,
    ))
    assert result == {"break_even_users": 9}


def test_compute_break_even_tool_rejects_non_positive():
    reset_state()
    result = json.loads(tools.compute_break_even.run(
        estimated_build_cost=0, price_point_monthly=20, break_even_horizon_months=6,
    ))
    assert "error" in result


# --- tools.py: distilled knowledge base (four-fixes addendum, point 3) -----

def test_write_knowledge_entry_requires_topic_and_takeaway():
    reset_state()
    result = json.loads(tools.write_knowledge_entry.run(
        topic="  ", takeaway="works well", confidence="moderate",
        source_hypothesis_ids=json.dumps(["hyp_x"]),
    ))
    assert "error" in result

    result = json.loads(tools.write_knowledge_entry.run(
        topic="Reddit organic", takeaway="  ", confidence="moderate",
        source_hypothesis_ids=json.dumps(["hyp_x"]),
    ))
    assert "error" in result


def test_write_knowledge_entry_rejects_invalid_confidence():
    reset_state()
    result = json.loads(tools.write_knowledge_entry.run(
        topic="Reddit organic", takeaway="weak below ~50 karma", confidence="certain",
        source_hypothesis_ids=json.dumps(["hyp_x"]),
    ))
    assert "error" in result


def test_write_knowledge_entry_requires_nonempty_source_ids():
    reset_state()
    result = json.loads(tools.write_knowledge_entry.run(
        topic="Reddit organic", takeaway="weak below ~50 karma", confidence="moderate",
        source_hypothesis_ids=json.dumps([]),
    ))
    assert "error" in result

    result = json.loads(tools.write_knowledge_entry.run(
        topic="Reddit organic", takeaway="weak below ~50 karma", confidence="moderate",
        source_hypothesis_ids="not a json array",
    ))
    assert "error" in result


def test_write_knowledge_entry_and_read_knowledge_base_roundtrip():
    reset_state()
    result = json.loads(tools.write_knowledge_entry.run(
        topic="Reddit organic on r/algotrading", takeaway="tested 4x, weak below ~50 karma accounts",
        confidence="moderate", source_hypothesis_ids=json.dumps(["hyp_a", "hyp_b"]),
        channel="reddit", tactic="thread_reply",
    ))
    assert result["ok"] is True

    all_entries = json.loads(tools.read_knowledge_base.run())
    assert len(all_entries) == 1
    assert all_entries[0]["confidence"] == "moderate"
    assert all_entries[0]["source_hypothesis_ids"] == ["hyp_a", "hyp_b"]

    assert len(json.loads(tools.read_knowledge_base.run(topic="reddit organic"))) == 1  # case-insensitive substring
    assert json.loads(tools.read_knowledge_base.run(topic="cold email")) == []
    assert len(json.loads(tools.read_knowledge_base.run(channel="reddit"))) == 1
    assert json.loads(tools.read_knowledge_base.run(channel="discord")) == []


def test_evaluate_hypothesis_requires_break_even_users():
    reset_state()
    hyp = {k: v for k, v in SAMPLE_HYP.items() if k != "break_even_users"}
    tools.write_hypothesis.run(hypothesis=json.dumps({**hyp, "break_even_users": None}))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "measured": {"reach_estimate": 1000, "reach_source": "estimated_upvotes"},
    }))
    result = json.loads(tools.evaluate_hypothesis.run(hypothesis_id="hyp_test_0001"))
    assert "error" in result and "break_even_users" in result["error"]


def test_evaluate_hypothesis_returns_build_outcome_when_sample_clears_break_even():
    reset_state()
    hyp = {**SAMPLE_HYP, "break_even_users": 2, "failure_rate": 0.001, "success_rate": 0.01}
    tools.write_hypothesis.run(hypothesis=json.dumps(hyp))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "measured": {"reach_estimate": 200, "reach_source": "estimated_upvotes"},
    }))
    created_at = datetime.fromisoformat(
        json.loads(tools.read_hypotheses.run())[0]["created_at"].replace("Z", "+00:00")
    )
    inside_ts = (created_at + timedelta(hours=1)).isoformat()
    for i in range(2):  # exactly break_even_users - a tiny sample, but real
        tools._append_jsonl("signups.jsonl", {
            "issue_number": i, "landing_page_variant_id": "lp_v1_default", "submitted_at": inside_ts,
        })
    result = json.loads(tools.evaluate_hypothesis.run(hypothesis_id="hyp_test_0001"))
    assert result["score"] >= 0.7, result
    assert result["outcome"] == "build", result


def test_evaluate_hypothesis_returns_test_further_when_score_good_but_sample_too_small():
    reset_state()
    hyp = {**SAMPLE_HYP, "break_even_users": 50, "failure_rate": 0.001, "success_rate": 0.01}
    tools.write_hypothesis.run(hypothesis=json.dumps(hyp))
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "measured": {"reach_estimate": 200, "reach_source": "estimated_upvotes"},
    }))
    created_at = datetime.fromisoformat(
        json.loads(tools.read_hypotheses.run())[0]["created_at"].replace("Z", "+00:00")
    )
    inside_ts = (created_at + timedelta(hours=1)).isoformat()
    for i in range(2):  # same great rate as above, but break_even_users=50 this time
        tools._append_jsonl("signups.jsonl", {
            "issue_number": i, "landing_page_variant_id": "lp_v1_default", "submitted_at": inside_ts,
        })
    result = json.loads(tools.evaluate_hypothesis.run(hypothesis_id="hyp_test_0001"))
    assert result["score"] >= 0.7, result
    assert result["outcome"] == "test_further", "a good rate on too few real conversions must not be 'build'"


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


def test_write_channel_rejects_duplicate_name_under_different_id():
    reset_state()
    first = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "ch_reddit_quantfinance", "name": "r/quantfinance", "category": "community_marketing",
        "is_paid": False, "impact_score": 8, "confidence_score": 8,
    })))
    assert first["ok"] is True

    # same name, different id, even different casing/whitespace - rejected
    dup = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "ch_reddit_quantfinance_2", "name": "  r/QuantFinance  ", "category": "community_marketing",
        "is_paid": False, "impact_score": 7, "confidence_score": 7,
    })))
    assert "error" in dup and "ch_reddit_quantfinance" in dup["error"]
    stored = json.loads(tools.read_channels.run())
    # reset_state() already seeds one "reddit" channel - no near-duplicate
    # roster entry beyond that plus the one we successfully created
    assert len(stored) == 2

    # updating the SAME id (not creating a new one) still works fine
    update = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "ch_reddit_quantfinance", "impact_score": 9,
    })))
    assert update["ok"] is True


def test_write_channel_total_roster_cap_enforced():
    reset_state()
    for i in range(tools.MAX_TOTAL_CHANNELS - 1):  # reset_state already seeded 1 ("reddit")
        tools.write_channel.run(channel=json.dumps({
            "id": f"candidate_{i}", "name": f"Candidate {i}", "category": "content_marketing",
            "is_paid": False, "impact_score": 3, "confidence_score": 3,
        }))
    assert len(json.loads(tools.read_channels.run())) == tools.MAX_TOTAL_CHANNELS
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "one_too_many", "name": "One Too Many", "category": "seo",
        "is_paid": False, "impact_score": 1, "confidence_score": 1,
    })))
    assert "error" in result and "max 20" in result["error"]
    # updating an existing channel must still work even when the roster is full
    result = json.loads(tools.write_channel.run(
        channel=json.dumps({"id": "reddit", "status": "bench"}), reason="still allowed at cap",
    ))
    assert result.get("ok") is True


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


def test_write_channel_paid_rejects_when_policy_disallows():
    reset_state()
    # default policy: paid_channels_allowed=False - blocked before the
    # approval-queue check even runs, regardless of approved_request_id.
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
    })))
    assert "error" in result and "paid_channels_allowed" in result["error"]


def test_write_channel_paid_requires_approved_spend_request():
    reset_state()
    _allow_paid_channels()
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
    })))
    assert "error" in result and "approved_request_id" in result["error"]


def test_write_channel_paid_succeeds_with_approved_spend_request():
    reset_state()
    _allow_paid_channels()
    appr = json.loads(tools.request_approval.run(category="spend", proposal="$1500 reddit ads test", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[-1]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)
    result = json.loads(tools.write_channel.run(channel=json.dumps({
        "id": "reddit_ads", "name": "Reddit Ads", "category": "paid_ads", "is_paid": True,
        "impact_score": 3, "confidence_score": 2, "status": "testing",
        "approved_request_id": appr["queued"],
    })))
    assert result == {"ok": True, "id": "reddit_ads", "status": "testing"}


def test_write_channel_paid_rejects_pending_approval():
    reset_state()
    _allow_paid_channels()
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


# --- tools.py: research-evidence tier --------------------------------------

def test_log_research_finding_rejects_invalid_type():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="made_up", source="s", summary="text",
    ))
    assert "error" in result


def test_log_research_finding_requires_summary():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="forum_discussion", source="s", summary="  ",
    ))
    assert "error" in result


def test_log_research_finding_and_read_roundtrip():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="competitor_product",
        source="https://example.com/competitor", summary="Existing tool does X but not Y",
    ))
    assert result["ok"] is True
    found = json.loads(tools.read_research_findings.run(hypothesis_id="hyp_x"))
    assert len(found) == 1 and found[0]["finding_type"] == "competitor_product"
    assert json.loads(tools.read_research_findings.run(hypothesis_id="hyp_other")) == []


# --- tools.py: content drafting ---------------------------------------------

def _draft_kwargs(**overrides):
    kwargs = dict(
        hypothesis_id=SAMPLE_HYP["id"], platform="reddit", post_type="thread_reply",
        target_community="r/algotrading", text="Ran into the same issue last week, fixed it by checking the rate limit headers first.",
        is_promotional=False, include_product_link=False, rules_checked=True,
        rules_notes="self-promo allowed max 1/10 posts, checked the wiki just now",
    )
    kwargs.update(overrides)
    return kwargs


def test_draft_content_rejects_invalid_platform():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(platform="cold_email")))
    assert "error" in result


def test_draft_content_rejects_invalid_post_type():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(post_type="advertisement")))
    assert "error" in result


def test_draft_content_requires_rules_checked():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(rules_checked=False)))
    assert "error" in result and "rules_checked" in result["error"]


def test_draft_content_requires_rules_notes():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(rules_notes="  ")))
    assert "error" in result


def test_draft_content_rejects_markdown_header():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(text="## Summary\nThis fixes it.")))
    assert "error" in result and any("markdown header" in v for v in result["violations"])


def test_draft_content_rejects_bullet_list():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(text="Steps:\n- do this\n- then that")))
    assert "error" in result and any("bullet" in v for v in result["violations"])


def test_draft_content_rejects_ai_phrasing():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(text="Great question. In conclusion, this approach works well.")))
    assert "error" in result and any("in conclusion" in v for v in result["violations"])


def test_draft_content_rejects_over_length():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(text="x" * 700, post_type="thread_reply")))
    assert "error" in result and any("exceeds" in v for v in result["violations"])


def test_draft_content_rejects_unknown_hypothesis():
    reset_state()
    result = json.loads(tools.draft_content.run(**_draft_kwargs(hypothesis_id="hyp_does_not_exist")))
    assert "error" in result


def test_draft_content_link_requires_landing_page_live():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.draft_content.run(**_draft_kwargs(include_product_link=True)))
    assert "error" in result and "landing_page_live" in result["error"]


def test_draft_content_succeeds_and_read_content_drafts_roundtrip():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.draft_content.run(**_draft_kwargs()))
    assert result["ok"] is True and result["status"] == "drafted"
    drafts = json.loads(tools.read_content_drafts.run(hypothesis_id=SAMPLE_HYP["id"]))
    assert len(drafts) == 1 and drafts[0]["status"] == "drafted"
    assert json.loads(tools.read_content_drafts.run(status="posted")) == []


# --- tools.py: community risk + account stats -------------------------------

def test_check_community_risk_low_with_no_removals():
    reset_state()
    result = json.loads(tools.check_community_risk.run(platform="reddit", target_community="r/algotrading"))
    assert result["risk"] == "low" and result["removal_count_last_30d"] == 0


def test_check_community_risk_high_after_two_removals():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    now = datetime.now(timezone.utc).isoformat()
    for i in range(2):
        tools._append_jsonl("content_drafts.jsonl", {
            "id": f"draft_removed{i}", "platform": "reddit", "target_community": "r/algotrading",
            "removed": True, "removed_reason": "mod removed", "removed_at": now,
        })
    result = json.loads(tools.check_community_risk.run(platform="reddit", target_community="r/algotrading"))
    assert result["risk"] == "high" and result["removal_count_last_30d"] == 2


def test_get_account_stats_ratio():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    tools.draft_content.run(**_draft_kwargs(is_promotional=False))
    tools.draft_content.run(**_draft_kwargs(is_promotional=True, text="Just launched something that solves this - link in profile."))
    result = json.loads(tools.get_account_stats.run(platform="reddit"))
    assert result["total_posts_drafted"] == 2
    assert result["promotional_count"] == 1
    assert result["promotional_ratio"] == 0.5


# --- tools.py: write_hypothesis - new optional fields default cleanly ------

def test_write_hypothesis_defaults_new_optional_fields():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["landing_page_live"] is False
    assert stored["defensibility_notes"] is None
    assert stored["pricing_tier_reasoning"] is None
    assert stored["expansion_notes"] is None
    assert stored["channel_fit_reasoning"] is None


# --- tools.py: cross-cycle continuity + usage log ---------------------------

def test_cycle_note_roundtrip():
    reset_state()
    assert tools.read_last_cycle_note() == ""
    tools.save_cycle_note("cycle 1 happened")
    assert tools.read_last_cycle_note() == "cycle 1 happened"
    tools.save_cycle_note("cycle 2 happened")  # overwrites, only latest kept
    assert tools.read_last_cycle_note() == "cycle 2 happened"


def test_log_cycle_usage_appends():
    reset_state()
    tools.log_cycle_usage({"total_tokens": 100, "successful_requests": 5})
    tools.log_cycle_usage({"total_tokens": 200, "successful_requests": 8})
    history = tools._read_jsonl("usage_history.jsonl")
    assert len(history) == 2
    assert history[1]["total_tokens"] == 200
    assert "at" in history[0]


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


# --- tools.py: state-persistence check --------------------------------------

def _clear_railway_env():
    for key in ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH"):
        os.environ.pop(key, None)


def test_check_state_persistence_not_applicable_outside_railway():
    reset_state()
    had = {k: os.environ.pop(k, None) for k in
           ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH")}
    try:
        result = tools.check_state_persistence()
        assert result == {"applicable": False, "persistent": True, "warning": None}
    finally:
        for k, v in had.items():
            if v is not None:
                os.environ[k] = v


def test_check_state_persistence_detects_matching_volume():
    reset_state()
    had = {k: os.environ.pop(k, None) for k in
           ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH")}
    try:
        os.environ["RAILWAY_ENVIRONMENT_ID"] = "env_test"
        os.environ["RAILWAY_VOLUME_NAME"] = "data"
        os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = str(tools.STATE_DIR)
        result = tools.check_state_persistence()
        assert result["applicable"] is True
        assert result["persistent"] is True
        assert result["warning"] is None
    finally:
        _clear_railway_env()
        for k, v in had.items():
            if v is not None:
                os.environ[k] = v


def test_check_state_persistence_flags_missing_volume():
    reset_state()
    had = {k: os.environ.pop(k, None) for k in
           ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH")}
    try:
        os.environ["RAILWAY_ENVIRONMENT_ID"] = "env_test"
        # no RAILWAY_VOLUME_MOUNT_PATH at all - no volume attached
        result = tools.check_state_persistence()
        assert result["applicable"] is True
        assert result["persistent"] is False
        assert result["warning"] and str(tools.STATE_DIR) in result["warning"]
    finally:
        _clear_railway_env()
        for k, v in had.items():
            if v is not None:
                os.environ[k] = v


def test_check_state_persistence_flags_mismatched_mount_path():
    reset_state()
    had = {k: os.environ.pop(k, None) for k in
           ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH")}
    try:
        os.environ["RAILWAY_ENVIRONMENT_ID"] = "env_test"
        os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = "/some/other/path"
        result = tools.check_state_persistence()
        assert result["persistent"] is False
        assert result["warning"]
    finally:
        _clear_railway_env()
        for k, v in had.items():
            if v is not None:
                os.environ[k] = v


# --- tools.py: Telegram remote control ---------------------------------------

def test_system_pause_roundtrip():
    reset_state()
    assert tools.is_system_paused() == (False, "")
    tools.set_system_paused(True, "per Telegram-Kommando 'stop'")
    paused, note = tools.is_system_paused()
    assert paused is True and note == "per Telegram-Kommando 'stop'"
    tools.set_system_paused(False)
    assert tools.is_system_paused() == (False, "")


def test_process_telegram_commands_skips_without_credentials():
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        assert tools.process_telegram_commands() == []  # must not raise
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_fetch_telegram_updates_degrades_gracefully_on_bad_token():
    os.environ["TELEGRAM_BOT_TOKEN"] = "invalid-token-for-checkup"
    os.environ["TELEGRAM_CHAT_ID"] = "0"
    try:
        assert tools._fetch_telegram_updates() == []  # real network call, bad token -> must not raise
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)


def test_classify_command_stop_and_start():
    assert tools._classify_command("stop", "") == ("pause", None)
    assert tools._classify_command("  Pause  ", "") == ("pause", None)
    assert tools._classify_command("start", "") == ("resume", None)
    assert tools._classify_command("Resume", "") == ("resume", None)


def test_classify_command_approve_reject_via_reply():
    reply_text = "Neue Freigabe angefragt: appr_ab12cd34\nKategorie: spend"
    assert tools._classify_command("approve", reply_text) == ("approve", "appr_ab12cd34")
    assert tools._classify_command("ja", reply_text) == ("approve", "appr_ab12cd34")
    assert tools._classify_command("reject", reply_text) == ("reject", "appr_ab12cd34")
    assert tools._classify_command("nein", reply_text) == ("reject", "appr_ab12cd34")
    # a plain approve/reject with nothing to reply to isn't a recognized command
    assert tools._classify_command("approve", "") is None


def test_classify_command_approve_reject_via_typed_id():
    assert tools._classify_command("appr_ab12cd34 approve", "") == ("approve", "appr_ab12cd34")
    assert tools._classify_command("approve appr_ab12cd34", "") == ("approve", "appr_ab12cd34")
    assert tools._classify_command("appr_ab12cd34 reject", "") == ("reject", "appr_ab12cd34")


def test_classify_command_unrecognized_text_is_ignored():
    assert tools._classify_command("just chatting, not a command", "") is None
    assert tools._classify_command("appr_ab12cd34 maybe?", "") is None


def test_classify_command_live():
    assert tools._classify_command("live: hyp_test_0001", "") == ("live", "hyp_test_0001")
    assert tools._classify_command("live:", "") is None


def test_classify_command_posted():
    assert tools._classify_command("posted: draft_ab12cd34 https://reddit.com/r/x/y", "") == (
        "posted", ("draft_ab12cd34", "https://reddit.com/r/x/y")
    )
    assert tools._classify_command("posted: no draft id here", "") is None
    assert tools._classify_command("posted: draft_ab12cd34", "") is None  # no url


def test_classify_command_removed():
    assert tools._classify_command("removed: draft_ab12cd34 mod took it down", "") == (
        "removed", ("draft_ab12cd34", "mod took it down")
    )
    assert tools._classify_command("removed: draft_ab12cd34", "") == (
        "removed", ("draft_ab12cd34", "removed (no reason given)")
    )


def test_apply_telegram_commands_pause_and_resume():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        log = tools._apply_telegram_commands([{"text": "stop", "reply_to_text": ""}])
        assert tools.is_system_paused()[0] is True
        assert any("pausiert" in entry for entry in log)

        log = tools._apply_telegram_commands([{"text": "start", "reply_to_text": ""}])
        assert tools.is_system_paused()[0] is False
        assert any("fortgesetzt" in entry for entry in log)
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_approve_via_reply():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        appr = json.loads(tools.request_approval.run(category="publish", proposal="p", reasoning="r"))
        request_id = appr["queued"]
        notification_text = f"Neue Freigabe angefragt: {request_id}\nKategorie: publish"

        log = tools._apply_telegram_commands([{"text": "approve", "reply_to_text": notification_text}])
        assert any(request_id in entry and "approved" in entry for entry in log)
        stored = next(r for r in tools._read_jsonl("approval_queue.jsonl") if r["id"] == request_id)
        assert stored["status"] == "approved"

        # already decided - a second reply must not flip it or error
        log = tools._apply_telegram_commands([{"text": "reject", "reply_to_text": notification_text}])
        assert log == []
        stored = next(r for r in tools._read_jsonl("approval_queue.jsonl") if r["id"] == request_id)
        assert stored["status"] == "approved"
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_live_marks_hypothesis():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
        log = tools._apply_telegram_commands([{"text": f"live: {SAMPLE_HYP['id']}", "reply_to_text": ""}])
        assert any("landing_page_live" in entry for entry in log)
        stored = json.loads(tools.read_hypotheses.run())[0]
        assert stored["landing_page_live"] is True

        log = tools._apply_telegram_commands([{"text": "live: hyp_does_not_exist", "reply_to_text": ""}])
        assert log == []  # unrecognized target, no crash, nothing logged as applied
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_posted_and_removed_mark_draft():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
        drafted = json.loads(tools.draft_content.run(**_draft_kwargs()))
        draft_id = drafted["id"]

        log = tools._apply_telegram_commands([{
            "text": f"posted: {draft_id} https://reddit.com/r/algotrading/comments/xyz", "reply_to_text": "",
        }])
        assert any("gepostet" in entry for entry in log)
        stored = next(d for d in tools._read_jsonl("content_drafts.jsonl") if d["id"] == draft_id)
        assert stored["status"] == "posted" and stored["post_url"] == "https://reddit.com/r/algotrading/comments/xyz"

        log = tools._apply_telegram_commands([{"text": f"removed: {draft_id} mod took it down", "reply_to_text": ""}])
        assert any("entfernt" in entry for entry in log)
        stored = next(d for d in tools._read_jsonl("content_drafts.jsonl") if d["id"] == draft_id)
        assert stored["status"] == "removed" and stored["removed"] is True and stored["removed_reason"] == "mod took it down"
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_notify_new_pending_approvals_marks_and_is_idempotent():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        appr = json.loads(tools.request_approval.run(category="publish", proposal="p", reasoning="r"))
        tools.notify_new_pending_approvals()
        stored = next(r for r in tools._read_jsonl("approval_queue.jsonl") if r["id"] == appr["queued"])
        assert stored["telegram_notified"] is True

        # calling again must not error and must not un-mark it
        tools.notify_new_pending_approvals()
        stored = next(r for r in tools._read_jsonl("approval_queue.jsonl") if r["id"] == appr["queued"])
        assert stored["telegram_notified"] is True
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


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


# --- holding.py: subsidiary registry ----------------------------------------

def test_read_subsidiaries_auto_bootstraps_api_sentinel():
    reset_state()
    subs = json.loads(holding.read_subsidiaries.run())
    assert len(subs) == 1
    assert subs[0]["id"] == "api-sentinel"
    assert subs[0]["status"] == "active"
    assert subs[0]["state_dir"] == str(tools.STATE_DIR)


def test_read_subsidiaries_status_filter():
    reset_state()
    holding.read_subsidiaries.run()  # bootstraps api-sentinel
    assert len(json.loads(holding.read_subsidiaries.run(status="active"))) == 1
    assert len(json.loads(holding.read_subsidiaries.run(status="dormant"))) == 0


def test_register_subsidiary_requires_approved_spend_like_request():
    reset_state()
    result = json.loads(holding.register_subsidiary.run(
        subsidiary=json.dumps({"id": "second-co", "name": "Second Co", "focus": "test"}),
        approved_request_id="appr_doesnotexist",
    ))
    assert "error" in result and "approved_request_id" in result["error"]


def test_register_subsidiary_succeeds_once_approved():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="deploy", proposal="spin off second-co", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)

    result = json.loads(holding.register_subsidiary.run(
        subsidiary=json.dumps({"id": "second-co", "name": "Second Co", "focus": "test"}),
        approved_request_id=appr["queued"],
    ))
    assert result["ok"] is True and result["id"] == "second-co"
    assert result["policies"] == holding.SUBSIDIARY_POLICY_DEFAULTS
    subs = json.loads(holding.read_subsidiaries.run())
    assert {s["id"] for s in subs} == {"api-sentinel", "second-co"}


def test_register_subsidiary_rejects_duplicate_id():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)
    result = json.loads(holding.register_subsidiary.run(
        subsidiary=json.dumps({"id": "api-sentinel", "name": "dup", "focus": "test"}),
        approved_request_id=appr["queued"],
    ))
    assert "error" in result


def test_read_subsidiary_policies_defaults_to_conservative_baseline():
    reset_state()
    holding.read_subsidiaries.run()  # bootstraps api-sentinel
    policies = json.loads(holding.read_subsidiary_policies.run(subsidiary_id="api-sentinel"))
    assert policies == holding.SUBSIDIARY_POLICY_DEFAULTS


def test_read_subsidiary_policies_unknown_subsidiary():
    reset_state()
    result = json.loads(holding.read_subsidiary_policies.run(subsidiary_id="does-not-exist"))
    assert "error" in result


def test_update_subsidiary_policies_requires_approval():
    reset_state()
    holding.read_subsidiaries.run()
    result = json.loads(holding.update_subsidiary_policies.run(
        subsidiary_id="api-sentinel", policies_patch=json.dumps({"paid_channels_allowed": True}),
        approved_request_id="appr_doesnotexist", reasoning="r",
    ))
    assert "error" in result


def test_update_subsidiary_policies_rejects_unknown_keys():
    reset_state()
    holding.read_subsidiaries.run()
    result = json.loads(holding.update_subsidiary_policies.run(
        subsidiary_id="api-sentinel", policies_patch=json.dumps({"made_up_policy": True}),
        approved_request_id="appr_doesnotexist", reasoning="r",
    ))
    assert "error" in result


def test_update_subsidiary_policies_roundtrip():
    reset_state()
    holding.read_subsidiaries.run()
    appr = json.loads(tools.request_approval.run(category="pricing", proposal="allow paid channels", reasoning="r"))
    approvals = tools._read_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_jsonl("approval_queue.jsonl", approvals)

    result = json.loads(holding.update_subsidiary_policies.run(
        subsidiary_id="api-sentinel", policies_patch=json.dumps({"paid_channels_allowed": True}),
        approved_request_id=appr["queued"], reasoning="re-testing paid ads for this subsidiary",
    ))
    assert result["ok"] is True
    assert result["policies"]["paid_channels_allowed"] is True
    # other policies stay at their defaults, only the touched key changed
    assert result["policies"]["cold_email_allowed"] is False

    policies = json.loads(holding.read_subsidiary_policies.run(subsidiary_id="api-sentinel"))
    assert policies["paid_channels_allowed"] is True


def test_set_subsidiary_status_requires_reason():
    reset_state()
    holding.read_subsidiaries.run()
    result = json.loads(holding.set_subsidiary_status.run(subsidiary_id="api-sentinel", status="dormant", reason=""))
    assert "error" in result


def test_set_subsidiary_status_roundtrip():
    reset_state()
    holding.read_subsidiaries.run()
    result = json.loads(holding.set_subsidiary_status.run(
        subsidiary_id="api-sentinel", status="dormant", reason="project paused for the quarter",
    ))
    assert result == {"ok": True, "id": "api-sentinel", "status": "dormant"}
    subs = json.loads(holding.read_subsidiaries.run())
    assert subs[0]["status"] == "dormant"
    assert subs[0]["status_history"][-1]["reason"] == "project paused for the quarter"


# --- holding.py: pivot proposals --------------------------------------------

def test_file_pivot_proposal_requires_template_fields():
    reset_state()
    result = json.loads(holding.file_pivot_proposal.run(
        subsidiary_id="api-sentinel", proposal=json.dumps({"nature_of_change": "x"}),
    ))
    assert "error" in result


def test_file_and_decide_pivot_proposal():
    reset_state()
    filed = json.loads(holding.file_pivot_proposal.run(
        subsidiary_id="api-sentinel", proposal=json.dumps(SAMPLE_PIVOT),
    ))
    assert "filed" in filed
    pending = json.loads(holding.read_pivot_proposals.run(status="pending"))
    assert len(pending) == 1

    result = json.loads(holding.decide_pivot_proposal.run(
        proposal_id=filed["filed"], decision="approve_in_place", reasoning="scoped, low risk",
    ))
    assert result == {"ok": True, "id": filed["filed"], "decision": "approve_in_place"}
    decided = json.loads(holding.read_pivot_proposals.run(status="decided"))
    assert decided[0]["decision"] == "approve_in_place"


def test_decide_pivot_proposal_rejects_invalid_decision():
    reset_state()
    filed = json.loads(holding.file_pivot_proposal.run(
        subsidiary_id="api-sentinel", proposal=json.dumps(SAMPLE_PIVOT),
    ))
    result = json.loads(holding.decide_pivot_proposal.run(
        proposal_id=filed["filed"], decision="just_wing_it", reasoning="r",
    ))
    assert "error" in result


def test_decide_pivot_proposal_does_not_redecide():
    reset_state()
    filed = json.loads(holding.file_pivot_proposal.run(
        subsidiary_id="api-sentinel", proposal=json.dumps(SAMPLE_PIVOT),
    ))
    holding.decide_pivot_proposal.run(proposal_id=filed["filed"], decision="rejected", reasoning="r1")
    result = json.loads(holding.decide_pivot_proposal.run(
        proposal_id=filed["filed"], decision="approve_in_place", reasoning="r2",
    ))
    assert "error" in result
    decided = json.loads(holding.read_pivot_proposals.run())[0]
    assert decided["decision"] == "rejected", "first decision must stick"


# --- holding.py: cross-subsidiary requests ----------------------------------

def test_file_and_resolve_cross_subsidiary_request():
    reset_state()
    filed = json.loads(holding.file_cross_subsidiary_request.run(
        from_subsidiary_id="api-sentinel", to_subsidiary_id="second-co",
        request="reddit reach benchmarks", reasoning="no other subsidiary exists yet",
    ))
    assert "filed" in filed
    pending = json.loads(holding.read_cross_subsidiary_requests.run(status="pending"))
    assert len(pending) == 1

    result = json.loads(holding.resolve_cross_subsidiary_request.run(
        request_id=filed["filed"], decision="rejected",
        result="", reasoning="second-co does not exist yet",
    ))
    assert result == {"ok": True, "id": filed["filed"], "status": "rejected"}


def test_resolve_cross_subsidiary_request_rejects_invalid_decision():
    reset_state()
    filed = json.loads(holding.file_cross_subsidiary_request.run(
        from_subsidiary_id="api-sentinel", to_subsidiary_id="second-co", request="r", reasoning="r",
    ))
    result = json.loads(holding.resolve_cross_subsidiary_request.run(
        request_id=filed["filed"], decision="maybe",
    ))
    assert "error" in result


def test_resolve_cross_subsidiary_request_does_not_reresolve():
    reset_state()
    filed = json.loads(holding.file_cross_subsidiary_request.run(
        from_subsidiary_id="api-sentinel", to_subsidiary_id="second-co", request="r", reasoning="r",
    ))
    holding.resolve_cross_subsidiary_request.run(request_id=filed["filed"], decision="rejected")
    result = json.loads(holding.resolve_cross_subsidiary_request.run(request_id=filed["filed"], decision="approved"))
    assert "error" in result


# --- holding.py: research archive -------------------------------------------

def test_search_research_archive_finds_matching_hypothesis():
    reset_state()
    holding.read_subsidiaries.run()  # bootstraps api-sentinel with the real STATE_DIR
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_archive_1", "statement": "Freqtrade users want breaking-change alerts",
    }))
    result = json.loads(holding.search_research_archive.run(query="breaking-change"))
    assert result["total_matches"] == 1
    assert result["matches"][0]["subsidiary_id"] == "api-sentinel"
    assert result["matches"][0]["source_file"] == "hypotheses.jsonl"


def test_search_research_archive_empty_query_errors():
    reset_state()
    result = json.loads(holding.search_research_archive.run(query="   "))
    assert "error" in result


def test_search_research_archive_no_match():
    reset_state()
    holding.read_subsidiaries.run()
    result = json.loads(holding.search_research_archive.run(query="something that does not exist anywhere"))
    assert result["total_matches"] == 0
    assert result["matches"] == []


# --- holding.py: subsidiary trajectory (revenue-focus addendum, point 1b) --

def test_assess_subsidiary_trajectory_unknown_subsidiary():
    reset_state()
    result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="does-not-exist"))
    assert "error" in result


def _write_buried_hyp(i):
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": f"hyp_bury_{i}", "landing_page_variant_id": f"lp_v{i}_bury",
        "status": "buried", "outcome": "bury", "bury_reasoning": "weak signal",
    }))


def test_assess_subsidiary_trajectory_no_stall_below_threshold():
    reset_state()
    holding.read_subsidiaries.run()  # bootstraps api-sentinel with the real STATE_DIR
    for i in range(holding.STALL_RESOLVED_THRESHOLD - 1):
        _write_buried_hyp(i)
    result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
    assert result["resolved_count"] == holding.STALL_RESOLVED_THRESHOLD - 1
    assert result["possible_stall"] is False


def test_assess_subsidiary_trajectory_flags_possible_stall():
    reset_state()
    holding.read_subsidiaries.run()
    for i in range(holding.STALL_RESOLVED_THRESHOLD):
        _write_buried_hyp(i)
    result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
    assert result["resolved_count"] == holding.STALL_RESOLVED_THRESHOLD
    assert result["outcome_counts"]["bury"] == holding.STALL_RESOLVED_THRESHOLD
    assert result["outcome_counts"]["build"] == 0
    assert result["possible_stall"] is True


def test_assess_subsidiary_trajectory_no_stall_once_a_build_exists():
    reset_state()
    holding.read_subsidiaries.run()
    for i in range(holding.STALL_RESOLVED_THRESHOLD):
        _write_buried_hyp(i)
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_build_1", "landing_page_variant_id": "lp_v_build",
        "status": "evaluated", "outcome": "build",
    }))
    result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
    assert result["outcome_counts"]["build"] == 1
    assert result["possible_stall"] is False


# --- holding.py: status reports (Sub-CEO -> Main-CEO structured handoff) ----

def test_file_status_report_needs_decision_requires_context():
    reset_state()
    result = json.loads(holding.file_status_report.run(
        subsidiary_id="api-sentinel", what_was_asked="evaluate hyp_x",
        what_was_found="score 0.8, outcome build", needs_decision_from_above=True,
    ))
    assert "error" in result and "decision_context" in result["error"]


def test_file_status_report_rejects_invalid_outcome():
    reset_state()
    result = json.loads(holding.file_status_report.run(
        subsidiary_id="api-sentinel", what_was_asked="x", what_was_found="y", outcome="amazing",
    ))
    assert "error" in result


def test_file_and_read_status_report_flow():
    reset_state()
    filed = json.loads(holding.file_status_report.run(
        subsidiary_id="api-sentinel", what_was_asked="evaluate hyp_x",
        what_was_found="score 0.8, cleared break-even with 2 real conversions",
        hypothesis_id="hyp_x", outcome="build",
        needs_decision_from_above=True, decision_context="approve the deploy request appr_xxx",
    ))
    assert "filed" in filed

    needs_decision = json.loads(holding.read_status_reports.run(
        subsidiary_id="api-sentinel", needs_decision_only=True,
    ))
    assert len(needs_decision) == 1
    assert needs_decision[0]["outcome"] == "build"

    ack = json.loads(holding.acknowledge_status_report.run(report_id=filed["filed"]))
    assert ack.get("ok") is True
    stored = json.loads(holding.read_status_reports.run(subsidiary_id="api-sentinel"))[0]
    assert stored["acknowledged"] is True


def test_acknowledge_status_report_unknown_id():
    reset_state()
    result = json.loads(holding.acknowledge_status_report.run(report_id="report_doesnotexist"))
    assert "error" in result


# --- holding.py: strategic direction (Main-CEO -> Sub-CEO structured handoff)

def test_read_strategic_direction_none_set_is_valid():
    reset_state()
    result = json.loads(holding.read_strategic_direction.run(subsidiary_id="api-sentinel"))
    assert result == {"direction": None}


def test_set_and_read_strategic_direction_returns_latest():
    reset_state()
    holding.set_strategic_direction.run(
        subsidiary_id="api-sentinel", focus_area="prioritize value hypotheses", reasoning="early data is thin",
    )
    holding.set_strategic_direction.run(
        subsidiary_id="api-sentinel", focus_area="hold off on paid channels", reasoning="pivot proposal pending",
    )
    result = json.loads(holding.read_strategic_direction.run(subsidiary_id="api-sentinel"))
    assert result["direction"]["focus_area"] == "hold off on paid channels"


# --- crewai_patches.py: max_iter force-final-answer 400 fix ----------------

def test_max_iterations_patch_uses_user_role_not_assistant():
    from crewai.agents.crew_agent_executor import handle_max_iterations_exceeded
    from crewai.agents.parser import AgentFinish

    class _FakePrinter:
        def print(self, content, color=None):
            pass

    class _FakeLLM:
        def call(self, messages, callbacks=None):
            assert messages[-1]["role"] == "user", (
                f"expected the forced-final-answer message to be role='user' "
                f"(Anthropic rejects a conversation ending on 'assistant' as an "
                f"unsupported prefill), got '{messages[-1]['role']}'"
            )
            return "Final Answer: forced"

    messages = [{"role": "user", "content": "do the task"}]
    result = handle_max_iterations_exceeded(
        formatted_answer=AgentFinish(thought="t", output="partial", text="partial"),
        printer=_FakePrinter(),
        messages=messages,
        llm=_FakeLLM(),
        callbacks=[],
        verbose=False,
    )
    assert isinstance(result, AgentFinish)
    assert messages[-1]["role"] == "user"


def test_max_iterations_patch_applied_in_both_known_modules():
    import crewai.agents.crew_agent_executor as cae
    import crewai.experimental.agent_executor as eae
    # both modules import the name directly, so both need patching independently
    assert cae.handle_max_iterations_exceeded is eae.handle_max_iterations_exceeded


def test_strict_tools_patch_removes_strict_flag_from_schema():
    from crewai.utilities.agent_utils import convert_tools_to_openai_schema
    openai_tools, _, _ = convert_tools_to_openai_schema([tools.read_state, tools.write_hypothesis])
    assert len(openai_tools) == 2
    for schema in openai_tools:
        assert "strict" not in schema["function"], (
            "Anthropic rejects a request with 21+ tools marked 'strict' - "
            "the patch must strip this flag from every tool's schema"
        )


def test_strict_tools_patch_applied_in_both_known_modules():
    import crewai.agents.crew_agent_executor as cae
    from crewai.utilities import agent_utils
    assert agent_utils.convert_tools_to_openai_schema is cae.convert_tools_to_openai_schema


def test_ceo_agent_real_tool_schema_has_no_strict_flags():
    # Regression check tied to the actual production crash: ceo_agent's real
    # tool list (21 tools as of this addition) must never produce a schema
    # Anthropic would reject for exceeding its 20-strict-tools cap.
    from crewai.utilities.agent_utils import convert_tools_to_openai_schema
    openai_tools, _, _ = convert_tools_to_openai_schema(crew.ceo_agent.tools)
    assert len(openai_tools) == len(crew.ceo_agent.tools)
    assert all("strict" not in schema["function"] for schema in openai_tools)


# --- agent_profile.json: model/token/iteration profile toggle --------------

def test_agent_profile_file_has_both_profiles_fully_specified():
    with open(crew._AGENT_PROFILE_FILE, encoding="utf-8") as f:
        config = json.load(f)
    assert config["active_profile"] in config["profiles"]
    for name, profile in config["profiles"].items():
        assert "model" in profile and "cycle_token_budget" in profile, name
        for role in ("growth", "dev", "sub_ceo", "main_ceo"):
            agent_cfg = profile["agents"][role]
            for key in ("max_tokens", "max_iter", "max_execution_time"):
                assert isinstance(agent_cfg[key], int) and agent_cfg[key] > 0, (name, role, key)


def test_load_agent_profile_returns_the_active_one():
    profile = crew._load_agent_profile()
    with open(crew._AGENT_PROFILE_FILE, encoding="utf-8") as f:
        config = json.load(f)
    assert profile["name"] == config["active_profile"]
    assert profile["model"] == config["profiles"][config["active_profile"]]["model"]


def test_agents_are_configured_from_the_active_profile():
    profile = crew.AGENT_PROFILE
    assert crew.growth_llm.max_tokens == profile["agents"]["growth"]["max_tokens"]
    assert crew.dev_llm.max_tokens == profile["agents"]["dev"]["max_tokens"]
    assert crew.ceo_llm.max_tokens == profile["agents"]["sub_ceo"]["max_tokens"]
    assert crew.main_ceo_llm.max_tokens == profile["agents"]["main_ceo"]["max_tokens"]
    assert crew.growth_agent.max_iter == profile["agents"]["growth"]["max_iter"]
    assert crew.dev_agent.max_iter == profile["agents"]["dev"]["max_iter"]
    assert crew.ceo_agent.max_iter == profile["agents"]["sub_ceo"]["max_iter"]
    assert crew.main_ceo_agent.max_iter == profile["agents"]["main_ceo"]["max_iter"]
    assert crew.CYCLE_TOKEN_BUDGET == profile["cycle_token_budget"]
    for llm in (crew.growth_llm, crew.dev_llm, crew.ceo_llm, crew.main_ceo_llm):
        assert llm.model == profile["model"]


# --- crew.py: construction sanity (no kickoff, no API calls) ---------------

def test_crew_has_four_agents_and_five_tasks():
    assert len(crew.crew.agents) == 4
    assert len(crew.crew.tasks) == 5


def test_ceo_agent_tools_match_spec():
    tool_names = {t.name for t in crew.ceo_agent.tools}
    assert tool_names == {
        "read_state", "read_hypotheses", "read_due_hypotheses", "write_hypothesis", "evaluate_hypothesis",
        "check_escalation", "compare_channel_performance", "request_approval",
        "read_channels", "write_channel", "compute_break_even",
        "file_task_order", "read_task_orders",
        "file_status_report", "read_strategic_direction",
        "file_pivot_proposal", "file_cross_subsidiary_request", "search_research_archive",
        "read_subsidiary_policies", "read_content_drafts", "log_research_finding", "read_research_findings",
        "read_knowledge_base", "write_knowledge_entry",
    }, tool_names


def test_main_ceo_agent_tools_match_spec():
    tool_names = {t.name for t in crew.main_ceo_agent.tools}
    assert tool_names == {
        "read_subsidiaries", "register_subsidiary", "set_subsidiary_status",
        "read_pivot_proposals", "decide_pivot_proposal",
        "read_cross_subsidiary_requests", "resolve_cross_subsidiary_request",
        "read_status_reports", "acknowledge_status_report", "set_strategic_direction",
        "read_strategic_direction", "assess_subsidiary_trajectory",
        "search_research_archive", "request_approval",
        "read_subsidiary_policies", "update_subsidiary_policies",
    }, tool_names


def test_growth_dev_tools():
    assert {t.name for t in crew.growth_agent.tools} == {
        "request_approval", "read_channel_metrics", "read_channels", "read_state", "read_hypotheses",
        "read_task_orders", "complete_task_order", "draft_content", "read_content_drafts",
        "check_community_risk", "get_account_stats", "log_research_finding", "read_research_findings",
        "read_subsidiary_policies", "read_knowledge_base",
    }
    assert {t.name for t in crew.dev_agent.tools} == {
        "open_pull_request", "read_task_orders", "complete_task_order", "check_approval_status",
    }


def test_channel_strategy_task_assigned_to_ceo():
    assert crew.task_channel_strategy.agent is crew.ceo_agent
    assert crew.crew.tasks[0] is crew.task_channel_strategy


def test_main_ceo_review_task_assigned_to_main_ceo():
    assert crew.task_main_ceo_review.agent is crew.main_ceo_agent
    assert crew.crew.tasks[3] is crew.task_main_ceo_review


def test_agents_have_explicit_retry_limit():
    # crewai defaults to 2 (3 total attempts, each with a full fresh
    # max_iter/max_execution_time) if left unset - verify it's pinned to 1
    # on every agent instead of silently drifting back to that default.
    for agent in (crew.growth_agent, crew.dev_agent, crew.ceo_agent, crew.main_ceo_agent):
        assert agent.max_retry_limit == 1


def test_only_first_task_is_unconditional():
    from crewai.tasks.conditional_task import ConditionalTask
    assert not isinstance(crew.task_channel_strategy, ConditionalTask)
    for task in (crew.task_growth, crew.task_ceo, crew.task_main_ceo_review, crew.task_dev):
        assert isinstance(task, ConditionalTask)
        assert task.condition is crew._within_cycle_budget


def test_cycle_token_budget_gate_allows_under_budget():
    from crewai import Crew
    crew._limit_hits.clear()
    original = Crew.calculate_usage_metrics
    try:
        Crew.calculate_usage_metrics = lambda self: type("U", (), {"total_tokens": 0})()
        assert crew._within_cycle_budget(None) is True
        assert crew._limit_hits == []
    finally:
        Crew.calculate_usage_metrics = original
        crew._limit_hits.clear()


def test_cycle_token_budget_gate_blocks_over_budget():
    from crewai import Crew
    crew._limit_hits.clear()
    original = Crew.calculate_usage_metrics
    try:
        over_budget = crew.CYCLE_TOKEN_BUDGET + 1
        Crew.calculate_usage_metrics = lambda self: type("U", (), {"total_tokens": over_budget})()
        assert crew._within_cycle_budget(None) is False
        assert any("Zyklus-Token-Budget" in hit for hit in crew._limit_hits)
    finally:
        Crew.calculate_usage_metrics = original
        crew._limit_hits.clear()


def test_send_cycle_summary_never_raises_without_a_crew_run():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        crew.send_cycle_summary()  # no kickoff() happened; task.output is None on every task
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token


def test_usage_headline_and_detail_split():
    # _usage_line() used to fold "X tokens gesamt" mid-sentence into the
    # agent-profile line - split into a standalone headline (checked here)
    # plus the fuller detail line, per the token-total-upfront addendum.
    # The headline addendum (cost breakdown) later added cost_usd/budget%
    # to this same headline.
    usage = {
        "total_tokens": 12345, "prompt_tokens": 10000, "completion_tokens": 2345,
        "cached_prompt_tokens": 500, "cache_creation_tokens": 200, "successful_requests": 7,
        "cost_usd": 0.0456,
    }
    headline = crew._usage_headline(usage)
    assert headline.startswith("Gesamt-Tokens diesen Zyklus: 12345")
    assert "$0.0456" in headline
    assert "%" in headline
    assert crew._usage_headline(None) == "Gesamt-Tokens diesen Zyklus: nicht verfuegbar"
    detail = crew._usage_detail_line(usage)
    assert "10000 prompt" in detail and "2345 completion" in detail
    assert crew._usage_detail_line(None) == "LLM-Nutzung: nicht verfuegbar"


def test_usage_headline_is_first_line_in_cycle_summary():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    captured = []
    original_send = crew.send_telegram_message
    original_metrics = crew.crew.usage_metrics
    try:
        crew.send_telegram_message = lambda text, parse_mode=None: captured.append((text, parse_mode))
        crew.crew.usage_metrics = type("U", (), {
            "total_tokens": 999, "prompt_tokens": 900, "completion_tokens": 99,
            "cached_prompt_tokens": 0, "cache_creation_tokens": 0, "successful_requests": 3,
        })()
        crew.send_cycle_summary()
        assert captured, "expected send_telegram_message to be called"
        main_report = captured[0][0]
        lines = main_report.split("\n")
        assert lines[0].startswith("API Sentinel Zyklus - ")
        assert lines[1].startswith("Gesamt-Tokens diesen Zyklus: 999")
        assert "$" in lines[1]  # cost figure present (model is priced in the active testing profile)
        # the usage table is sent as its own follow-up, formatted message
        assert len(captured) == 2
        assert captured[1][1] == "Markdown"
        assert "```" in captured[1][0]
    finally:
        crew.send_telegram_message = original_send
        crew.crew.usage_metrics = original_metrics
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token


def test_format_usage_table_contains_key_figures():
    usage = {
        "total_tokens": 12345, "prompt_tokens": 10000, "completion_tokens": 2345,
        "cached_prompt_tokens": 500, "cache_creation_tokens": 200, "successful_requests": 7,
        "cost_usd": 0.0456,
    }
    table = crew._format_usage_table(usage)
    assert table.startswith("```\n") and table.endswith("\n```")
    assert "12345" in table
    assert "$0.0456" in table
    assert "Growth" in table and "Sub-CEO" in table and "Main-CEO" in table
    assert crew._format_usage_table(None) == ""


def test_malformed_tool_call_handler_records_events():
    crew._malformed_tool_calls.clear()
    fake_event = type("E", (), {
        "tool_name": "write_channel", "tool_args": {}, "error": "Field required",
    })()
    crew._on_tool_validate_input_error(source=None, event=fake_event)
    assert len(crew._malformed_tool_calls) == 1
    assert crew._malformed_tool_calls[0]["tool_name"] == "write_channel"
    crew._malformed_tool_calls.clear()


def test_task_usage_watchdog_records_per_task_tokens():
    from crewai import Crew as _Crew
    original_calc = _Crew.calculate_usage_metrics
    crew._task_usage_log.clear()
    original_cumulative = crew._last_cumulative_tokens
    crew._last_cumulative_tokens = 0
    try:
        _Crew.calculate_usage_metrics = lambda self: type("U", (), {"total_tokens": 500})()
        watchdog = crew._make_iteration_watchdog(crew.growth_agent, "Growth (Test)")
        watchdog(None)
        assert crew._task_usage_log[-1] == {"task": "Growth (Test)", "tokens": 500}

        _Crew.calculate_usage_metrics = lambda self: type("U", (), {"total_tokens": 900})()
        watchdog2 = crew._make_iteration_watchdog(crew.ceo_agent, "Sub-CEO (Test)")
        watchdog2(None)
        assert crew._task_usage_log[-1] == {"task": "Sub-CEO (Test)", "tokens": 400}
    finally:
        _Crew.calculate_usage_metrics = original_calc
        crew._task_usage_log.clear()
        crew._last_cumulative_tokens = original_cumulative


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
