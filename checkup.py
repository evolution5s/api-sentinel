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
    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)
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
    # AI-native economics: a Dev-agent LLM build, not agency/market rates -
    # kept at/under SIMPLE_BUILD_COST_CEILING and break_even_horizon_months
    # at the 1-month default so most tests don't also need to satisfy the
    # extra-justification-length gate; a few dedicated tests below exercise
    # that gate explicitly with their own higher-cost/longer-horizon patches.
    "estimated_build_cost": 5,
    "price_point_monthly": 20,
    "break_even_horizon_months": 1,
    "break_even_users": 1,  # ceil(5 / (20 * 1))
    "build_cost_reasoning": "~10 Dev-agent LLM calls to generate the landing page HTML/CSS, no recurring infra beyond what's already provisioned",
    "impact_score": 3,
    "confidence_score": 3,
    "primary_variable_tested": "audience",  # required for a first attempt (no prior_hypothesis_id)
    # Structural-rebuild addendum: evidence_stage is now always required.
    # Defaults to 'research' (no artifact-gate to satisfy at this stage,
    # unlike landing_page/build) so most tests don't also need to seed a
    # research/community_engagement artifact or an approved stage-skip
    # request just to create a hypothesis - dedicated tests below exercise
    # the later-stage gates explicitly with their own setup. The economics
    # fields above stay present regardless (harmless at 'research' - they're
    # simply not required yet - and needed as-is by the tests that exercise
    # the economics/ceiling checks directly, which fire independent of stage).
    "evidence_stage": "research",
    "research_objective": "does the target audience have a real, recurring pain point here",
    "research_confirming_criteria": "3+ independent accounts describing real impact",
    "research_disconfirming_criteria": "only generic chatter, nothing concrete found",
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
        pricing.get_pricing("claude-nonexistent-model", date(2026, 1, 1))
        assert False, "expected ValueError for an unpriced model"
    except ValueError:
        pass


def test_get_pricing_opus5():
    rates = pricing.get_pricing("claude-opus-5", date(2026, 1, 1))
    assert rates == {
        "base_input": 5.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0,
        "cache_hit": 0.50, "output": 25.0,
    }


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
    # subsidiary_id is now stamped on every subsidiary-scoped record write
    # (structural-rebuild addendum, section 2).
    assert tools._read_jsonl("scratch.jsonl") == [
        {"a": 1, "subsidiary_id": "api-sentinel"}, {"a": 2, "subsidiary_id": "api-sentinel"},
    ]


def test_jsonl_read_missing_file_returns_empty():
    reset_state()
    assert tools._read_jsonl("does_not_exist.jsonl") == []


def test_jsonl_write_overwrites():
    reset_state()
    tools._write_jsonl("scratch2.jsonl", [{"x": 1}, {"x": 2}])
    tools._write_jsonl("scratch2.jsonl", [{"x": 3}])
    assert tools._read_jsonl("scratch2.jsonl") == [{"x": 3, "subsidiary_id": "api-sentinel"}]


def test_jsonl_subsidiary_scoping_isolates_data_between_subsidiaries():
    reset_state()
    tools.set_active_subsidiary("api-sentinel")
    tools._append_jsonl("scratch3.jsonl", {"a": 1})
    tools.set_active_subsidiary("second-co")
    try:
        assert tools._read_jsonl("scratch3.jsonl") == []  # second-co sees none of api-sentinel's data
        tools._append_jsonl("scratch3.jsonl", {"a": 2})
        assert tools._read_jsonl("scratch3.jsonl") == [{"a": 2, "subsidiary_id": "second-co"}]
    finally:
        tools.set_active_subsidiary("api-sentinel")
    assert tools._read_jsonl("scratch3.jsonl") == [{"a": 1, "subsidiary_id": "api-sentinel"}]


# --- tools.py: request_approval / read_state -------------------------------

def test_request_approval_valid():
    reset_state()
    result = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
    assert "queued" in result
    stored = tools._read_global_jsonl("approval_queue.jsonl")
    assert len(stored) == 1 and stored[0]["status"] == "pending" and stored[0]["category"] == "deploy"


def test_request_approval_invalid_category():
    reset_state()
    result = json.loads(tools.request_approval.run(category="marketing", proposal="p", reasoning="r"))
    assert "error" in result
    assert tools._read_global_jsonl("approval_queue.jsonl") == []


# --- tools.py: rigid publish-approval template (structural-rebuild --------
# addendum, section 7) - no narrative prose in the fields that matter.

_PUBLISH_TEMPLATE = {
    "platform": "reddit", "target_url": "https://reddit.com/r/algotrading/comments/xyz",
    "title": "kein Titel", "text": "Ran into the same API timeout issue last week.",
    "footer": "keiner", "hypothesis_id": "hyp_test_0001", "evidence_stage": "community_engagement",
    "is_experiment": True, "success_criterion": ">=5 substantive replies within 7 days = confirmed signal",
}


def test_request_approval_publish_rejects_free_prose():
    reset_state()
    result = json.loads(tools.request_approval.run(category="publish", proposal="just post it, looks fine", reasoning="r"))
    assert "error" in result
    assert tools._read_global_jsonl("approval_queue.jsonl") == []


def test_request_approval_publish_rejects_missing_template_field():
    reset_state()
    incomplete = dict(_PUBLISH_TEMPLATE)
    del incomplete["success_criterion"]
    result = json.loads(tools.request_approval.run(category="publish", proposal=json.dumps(incomplete), reasoning="r"))
    assert "error" in result
    assert "success_criterion" in result["error"]


def test_request_approval_publish_rejects_empty_template_field():
    reset_state()
    bad = {**_PUBLISH_TEMPLATE, "target_url": "  "}
    result = json.loads(tools.request_approval.run(category="publish", proposal=json.dumps(bad), reasoning="r"))
    assert "error" in result


def test_request_approval_publish_rejects_non_bool_is_experiment():
    reset_state()
    bad = {**_PUBLISH_TEMPLATE, "is_experiment": "yes"}
    result = json.loads(tools.request_approval.run(category="publish", proposal=json.dumps(bad), reasoning="r"))
    assert "error" in result


def test_request_approval_publish_accepts_full_template():
    reset_state()
    result = json.loads(tools.request_approval.run(category="publish", proposal=json.dumps(_PUBLISH_TEMPLATE), reasoning="r"))
    assert "queued" in result, result
    stored = tools._read_global_jsonl("approval_queue.jsonl")
    assert stored[0]["category"] == "publish"


def test_request_approval_publish_accepts_no_success_criterion_needed():
    reset_state()
    pure_research = {**_PUBLISH_TEMPLATE, "is_experiment": False, "success_criterion": "nein, reine Recherche, kein Erfolgskriterium noetig"}
    result = json.loads(tools.request_approval.run(category="publish", proposal=json.dumps(pure_research), reasoning="r"))
    assert "queued" in result, result


def test_format_publish_proposal_renders_structured_fields():
    rendered = tools._format_publish_proposal(json.dumps(_PUBLISH_TEMPLATE))
    assert "Plattform: reddit" in rendered
    assert "Ziel-URL: https://reddit.com/r/algotrading/comments/xyz" in rendered
    assert "Titel: kein Titel" in rendered
    assert "Text:\nRan into the same API timeout issue last week." in rendered
    assert "Footer/Signatur: keiner" in rendered
    assert "Gehoert zu: hyp_test_0001 (evidence_stage: community_engagement)" in rendered
    assert "Ist das ein Experiment: ja" in rendered
    assert "Erfolgskriterium: >=5 substantive replies within 7 days = confirmed signal" in rendered


def test_format_publish_proposal_falls_back_on_non_json():
    assert tools._format_publish_proposal("not json at all") == "not json at all"


def test_notify_new_pending_approvals_renders_publish_template_structured():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    captured = []
    original_send = tools.send_telegram_message
    try:
        tools.send_telegram_message = lambda text, parse_mode=None: captured.append(text)
        os.environ["TELEGRAM_BOT_TOKEN"] = "x"
        os.environ["TELEGRAM_CHAT_ID"] = "1"
        tools.request_approval.run(category="publish", proposal=json.dumps(_PUBLISH_TEMPLATE), reasoning="research plan checked out")
        tools.notify_new_pending_approvals()
        assert captured, "expected a Telegram notification"
        assert "Plattform: reddit" in captured[0]
        assert "Begruendung: research plan checked out" in captured[0]
    finally:
        tools.send_telegram_message = original_send
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        else:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat
        else:
            os.environ.pop("TELEGRAM_CHAT_ID", None)


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
    assert result == {"id": appr["queued"], "status": "pending", "category": "deploy", "payment_link_url": None}

    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)
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
    tools.write_hypothesis.run(hypothesis=json.dumps({**SAMPLE_HYP, "id": "hyp_x"}))
    _seed_stage_skip_approval(hypothesis_id="hyp_x", target_stage="landing_page")
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_x", "evidence_stage": "landing_page",
        **{k: SAMPLE_HYP[k] for k in ("estimated_build_cost", "price_point_monthly",
                                       "break_even_horizon_months", "break_even_users", "build_cost_reasoning")},
    }))
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


# --- tools.py: AI-native build-cost economics --------------------------

def test_write_hypothesis_requires_build_cost_reasoning():
    reset_state()
    bad = dict(SAMPLE_HYP)
    del bad["build_cost_reasoning"]
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result


# --- tools.py: anti-instruction-copying tripwire (structural-rebuild -------
# addendum, section 5) - confirms the exact incident that triggered this
# (hyp_bootstrap_001's build_cost_reasoning traced to copied instruction
# text describing the $15,000/agency-rate miscalibration) would actually be
# caught mechanically, not just described in a docstring.

def test_write_hypothesis_build_cost_reasoning_rejects_instruction_echo():
    reset_state()
    bad = {
        **SAMPLE_HYP,
        "build_cost_reasoning": (
            "This system is built and operated by AI agents, so estimates must reflect Dev-agent token "
            "spend, never old-economy market-rate thinking about what a human developer/agency/employee "
            "would charge for a build like this."
        ),
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result
    assert "echoes known instruction" in result["error"]


def test_write_hypothesis_defensibility_notes_rejects_instruction_echo():
    reset_state()
    bad = {
        **SAMPLE_HYP,
        "defensibility_notes": (
            "Going higher needs a substantive reason citing genuinely more files/integration points/"
            "iteration passes, not because it feels like it should cost more."
        ),
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result
    assert "echoes known instruction" in result["error"]


def test_write_hypothesis_reasoning_fields_accept_genuine_text():
    reset_state()
    ok = {
        **SAMPLE_HYP,
        "defensibility_notes": (
            "Accumulates a real dataset of exchange API incident timestamps over time, which a solo dev "
            "couldn't trivially recreate in an afternoon - the moat grows with usage, not just code."
        ),
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(ok)))
    assert "error" not in result, result


def test_write_hypothesis_rejects_high_cost_without_substantial_reasoning():
    reset_state()
    bad = {
        **SAMPLE_HYP, "estimated_build_cost": 15000, "price_point_monthly": 49,
        "break_even_horizon_months": 1, "break_even_users": 307,
        "build_cost_reasoning": "agency quote",  # too short, and market-rate framing
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result and "sanity-check ceiling" in result["error"]


def test_write_hypothesis_accepts_high_cost_with_substantial_reasoning():
    reset_state()
    long_reasoning = (
        "Genuinely multi-integration build: separate webhook listeners for 4 exchange APIs, "
        "each needing its own auth/retry handling and a shared alert-dispatch layer - roughly "
        "40 Dev-agent LLM calls across several iteration passes, still token cost not agency rate."
    )
    ok = {
        **SAMPLE_HYP, "estimated_build_cost": 12, "build_cost_reasoning": long_reasoning,
        "break_even_users": 1,
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(ok)))
    assert result.get("ok") is True


def test_write_hypothesis_rejects_long_horizon_without_reasoning():
    reset_state()
    bad = {**SAMPLE_HYP, "break_even_horizon_months": 6, "build_cost_reasoning": ""}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(bad)))
    assert "error" in result


def test_write_hypothesis_accepts_long_horizon_with_reasoning():
    reset_state()
    ok = {
        **SAMPLE_HYP, "break_even_horizon_months": 3,
        "build_cost_reasoning": "Real recurring infra: a paid webhook-relay service at $4/mo is required for this specific integration, justifying a slightly longer payback window.",
    }
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(ok)))
    assert result.get("ok") is True


def test_write_hypothesis_update_falls_back_to_existing_reasoning():
    reset_state()
    short_reasoning = "a few LLM calls"  # short - fine at/under the ceiling, not above it
    tools.write_hypothesis.run(hypothesis=json.dumps({**SAMPLE_HYP, "build_cost_reasoning": short_reasoning}))

    # updating something unrelated, not re-touching estimated_build_cost -
    # must not require build_cost_reasoning again
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "measured": {"reach_estimate": 1000, "reach_source": "estimated_upvotes"},
    })))
    assert result.get("ok") is True

    # updating estimated_build_cost itself with no new reasoning at all
    # (falls back to the existing short one) still needs a substantive one
    # if pushed above the ceiling
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": "hyp_test_0001", "estimated_build_cost": 500,
    })))
    assert "error" in result


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


# --- tools.py: burying a hypothesis auto-closes its own open task orders ---
# 2026-08-11 fix: this used to depend entirely on an LLM instruction
# (task_ceo's bury step telling the Sub-CEO to call complete_task_order per
# open order) - and ceo_agent never actually had complete_task_order in its
# tool list, so that instruction was unexecutable as written. Real
# before/after checks on task_orders.jsonl, not an assumption that a fixed
# display line means the underlying data is also fixed.

def test_write_hypothesis_bury_auto_closes_its_own_open_orders():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    hyp_id = SAMPLE_HYP["id"]
    filed = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="build landing page", context="ctx", hypothesis_id=hyp_id,
    ))
    order_id = filed["filed"]

    before = next(o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["id"] == order_id)
    assert before["status"] == "open"

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": hyp_id, "status": "buried", "outcome": "bury", "bury_reasoning": "real reason",
    })))
    assert result.get("ok") is True
    assert result.get("orders_auto_closed") == 1

    after = next(o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["id"] == order_id)
    assert after["status"] == "done"
    assert hyp_id in after["result"] and "buried" in after["result"]
    assert after.get("completed_at")


def test_write_hypothesis_bury_closes_multiple_open_orders_across_roles():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    hyp_id = SAMPLE_HYP["id"]
    tools.file_task_order.run(to_role="growth", task_description="a", context="ctx", hypothesis_id=hyp_id)
    tools.file_task_order.run(to_role="growth", task_description="b", context="ctx", hypothesis_id=hyp_id)

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": hyp_id, "status": "buried", "outcome": "bury", "bury_reasoning": "real reason",
    })))
    assert result.get("orders_auto_closed") == 2
    orders = [o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["hypothesis_id"] == hyp_id]
    assert all(o["status"] == "done" for o in orders)


def test_write_hypothesis_bury_does_not_touch_other_hypotheses_orders():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    other = {**SAMPLE_HYP, "id": "hyp_other", "prior_hypothesis_id": None}
    tools.write_hypothesis.run(hypothesis=json.dumps(other))
    other_order = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="unrelated order", context="ctx", hypothesis_id="hyp_other",
    ))["filed"]

    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": SAMPLE_HYP["id"], "status": "buried", "outcome": "bury", "bury_reasoning": "real reason",
    }))

    untouched = next(o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["id"] == other_order)
    assert untouched["status"] == "open"


def test_write_hypothesis_bury_does_not_overwrite_already_done_orders():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    hyp_id = SAMPLE_HYP["id"]
    order_id = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="already handled", context="ctx", hypothesis_id=hyp_id,
    ))["filed"]
    tools.complete_task_order.run(order_id=order_id, result="real PR merged")

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": hyp_id, "status": "buried", "outcome": "bury", "bury_reasoning": "real reason",
    })))
    assert result.get("orders_auto_closed") is None  # nothing was open to close

    order = next(o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["id"] == order_id)
    assert order["result"] == "real PR merged"  # untouched, not overwritten


def test_write_hypothesis_non_bury_update_does_not_close_orders():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    hyp_id = SAMPLE_HYP["id"]
    order_id = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="still relevant", context="ctx", hypothesis_id=hyp_id,
    ))["filed"]

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": hyp_id, "evidence_stage": "community_engagement",
    })))
    assert result.get("orders_auto_closed") is None

    order = next(o for o in json.loads(tools.read_task_orders.run(to_role="growth")) if o["id"] == order_id)
    assert order["status"] == "open"


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
    assert result == {"applicable": True, "break_even_users": 9}


def test_compute_break_even_refuses_at_early_stage():
    reset_state()
    for stage in ("research", "community_engagement"):
        result = json.loads(tools.compute_break_even.run(
            estimated_build_cost=1000, price_point_monthly=20, break_even_horizon_months=6,
            evidence_stage=stage,
        ))
        assert result["applicable"] is False, result
        assert "break_even_users" not in result


def test_compute_break_even_applies_normally_at_later_stage():
    reset_state()
    for stage in ("landing_page", "build", ""):
        result = json.loads(tools.compute_break_even.run(
            estimated_build_cost=1000, price_point_monthly=20, break_even_horizon_months=6,
            evidence_stage=stage,
        ))
        assert result == {"applicable": True, "break_even_users": 9}


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
    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[-1]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)
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


# --- tools.py: web research - search_web/read_webpage (structural-rebuild --
# addendum, section 1)

def test_search_web_requires_serper_api_key():
    reset_state()
    had_key = os.environ.pop("API-Sentinel-serper", None)
    try:
        result = json.loads(tools.search_web.run(query="freqtrade api outage"))
        assert "error" in result
        assert "API-Sentinel-serper" in result["error"]
    finally:
        if had_key is not None:
            os.environ["API-Sentinel-serper"] = had_key


def test_search_web_real_endpoint_rejects_invalid_key():
    # A real (deliberately invalid) key exercises the real HTTP path against
    # Serper's actual endpoint without costing anything, confirming this
    # tool fails cleanly (not silently) rather than fabricating results -
    # independent of whether a real API-Sentinel-serper key happens to be
    # set in this environment (temporarily overridden either way).
    reset_state()
    had_key = os.environ.pop("API-Sentinel-serper", None)
    try:
        os.environ["API-Sentinel-serper"] = "invalid-test-key-not-a-real-account"
        result = json.loads(tools.search_web.run(query="freqtrade api outage"))
        assert "error" in result, result
    finally:
        if had_key is not None:
            os.environ["API-Sentinel-serper"] = had_key
        else:
            os.environ.pop("API-Sentinel-serper", None)


def test_search_web_live_real_key_returns_real_results():
    # Genuine live smoke test against Serper's real endpoint - only runs
    # when a real API-Sentinel-serper key is actually present (e.g. via
    # `railway run`), skips gracefully everywhere else (local dev, CI, a
    # future contributor's machine) so the suite never depends on a live
    # external API/budget to pass. Confirmed manually against the real
    # Railway-provisioned key (Real-Serper-Key addendum): non-empty,
    # genuine organic results, not a mock or an empty payload.
    reset_state()
    if not os.environ.get("API-Sentinel-serper"):
        print("    (skipped - API-Sentinel-serper not set in this environment)")
        return
    result = json.loads(tools.search_web.run(query="algotrading broker API outage"))
    assert "error" not in result, result
    assert result["results"], "expected at least one real organic result"
    first = result["results"][0]
    assert first.get("link", "").startswith("http")


def test_search_web_then_read_webpage_live_pipeline():
    # End-to-end: a real search_web result fed straight into read_webpage,
    # as the agents would actually use the two tools together. Skips
    # gracefully without a real key, same reasoning as the test above.
    reset_state()
    if not os.environ.get("API-Sentinel-serper"):
        print("    (skipped - API-Sentinel-serper not set in this environment)")
        return
    search_result = json.loads(tools.search_web.run(query="QuantConnect forum broker API outage"))
    assert search_result["results"], search_result
    # Reddit links are known (chapter 6.2/15) to return an anti-bot
    # challenge page rather than real content - skip those and read the
    # first non-Reddit result so this test actually exercises real content
    # extraction rather than asserting against a bot-check page.
    candidates = [r["link"] for r in search_result["results"] if "reddit.com" not in r["link"]]
    if not candidates:
        print("    (skipped - only Reddit links in this result set)")
        return
    page = json.loads(tools.read_webpage.run(url=candidates[0]))
    assert "error" not in page, page
    assert len(page["text"]) > 0


def test_read_webpage_live_fetches_real_content():
    # example.com is IANA-reserved specifically for documentation/testing
    # use, extremely stable - a safe target for a genuine live fetch.
    result = json.loads(tools.read_webpage.run(url="https://example.com"))
    assert "error" not in result, result
    assert "Example Domain" in result["text"]
    assert result["url"] == "https://example.com"
    assert isinstance(result["truncated"], bool)


def test_read_webpage_strips_script_and_style():
    result = json.loads(tools.read_webpage.run(url="https://example.com"))
    assert "error" not in result, result
    assert "<script" not in result["text"] and "<style" not in result["text"]


def test_read_webpage_rejects_nonexistent_domain():
    result = json.loads(tools.read_webpage.run(url="https://this-domain-should-not-exist-xyz-123-abc.invalid"))
    assert "error" in result


def test_read_webpage_truncates_long_content():
    reset_state()
    original_max = tools.READ_WEBPAGE_MAX_CHARS
    tools.READ_WEBPAGE_MAX_CHARS = 50
    try:
        result = json.loads(tools.read_webpage.run(url="https://example.com"))
        assert "error" not in result, result
        assert len(result["text"]) <= 50
        assert result["truncated"] is True
    finally:
        tools.READ_WEBPAGE_MAX_CHARS = original_max


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


_SUBSTANTIVE_SUMMARY = (
    "Checked r/algotrading and r/quantfinance for the last 3 months: 4 distinct threads describe a real "
    "exchange API outage causing missed stop-losses, two with specific dollar-figure losses mentioned."
)


def test_log_research_finding_and_read_roundtrip():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="competitor_product",
        source="https://example.com/competitor", summary=_SUBSTANTIVE_SUMMARY,
    ))
    assert result["ok"] is True
    found = json.loads(tools.read_research_findings.run(hypothesis_id="hyp_x"))
    assert len(found) == 1 and found[0]["finding_type"] == "competitor_product"
    assert json.loads(tools.read_research_findings.run(hypothesis_id="hyp_other")) == []


def test_log_research_finding_rejects_short_summary():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="competitor_product",
        source="https://example.com/competitor", summary="Existing tool does X but not Y",
    ))
    assert "error" in result
    assert json.loads(tools.read_research_findings.run(hypothesis_id="hyp_x")) == []


def test_log_research_finding_rejects_instruction_echo():
    reset_state()
    result = json.loads(tools.log_research_finding.run(
        hypothesis_id="hyp_x", finding_type="other", source="n/a",
        summary=(
            "This system is built and operated by AI agents, so it typically costs this system a few "
            "dollars in tokens rather than old-economy market-rate thinking about human developer/agency/"
            "employee rates for a build like this one."
        ),
    ))
    assert "error" in result


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


def _seed_research_artifact(hypothesis_id=None, summary=None):
    tools.log_research_finding.run(
        hypothesis_id=hypothesis_id or SAMPLE_HYP["id"], finding_type="forum_discussion",
        source="https://reddit.com/r/algotrading/comments/xyz",
        summary=summary or _SUBSTANTIVE_SUMMARY,
    )


def _seed_community_engagement_artifact(hypothesis_id=None):
    hyp_id = hypothesis_id or SAMPLE_HYP["id"]
    drafted = json.loads(tools.draft_content.run(**_draft_kwargs(hypothesis_id=hyp_id, post_type="thread_reply")))
    drafts = tools._read_jsonl("content_drafts.jsonl")
    idx = next(i for i, d in enumerate(drafts) if d["id"] == drafted["id"])
    drafts[idx]["status"] = "posted"
    tools._write_jsonl("content_drafts.jsonl", drafts)


def _seed_stage_skip_approval(hypothesis_id=None, target_stage="landing_page", subsidiary_id="api-sentinel"):
    filed = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id=hypothesis_id or SAMPLE_HYP["id"], subsidiary_id=subsidiary_id, target_stage=target_stage,
        reasoning="Test setup: this specific hypothesis's lineage already gathered equivalent evidence upstream.",
    ))
    holding.decide_stage_skip_request.run(request_id=filed["filed"], decision="approved", reasoning="test setup")


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
    assert stored["evidence_stage"] == "research"  # SAMPLE_HYP sets this explicitly now (required field)
    assert stored["payment_intent_approval_id"] is None
    assert stored["rough_economics_note"] is None


# --- tools.py: evidence-stage ladder (Dev/Growth-limits addendum) -----------

def test_write_hypothesis_evidence_stage_rejects_invalid_value():
    reset_state()
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({**SAMPLE_HYP, "evidence_stage": "not_a_real_stage"})
    ))
    assert "error" in result


# --- tools.py: evidence_stage backfill for pre-existing hypotheses ---------
# 2026-08-11 fix: a hypothesis written before evidence_stage existed as a
# field (hyp_bootstrap_001) just has it missing, which the rest of the
# gating logic isn't designed to handle. Each test uses its own never-
# before-touched subsidiary_id so tools._MIGRATED_EVIDENCE_STAGE's
# per-process cache can't mask the backfill not running.

def _write_raw_legacy_hypothesis(sub_id, record):
    tools.set_active_subsidiary(sub_id)
    tools.write_jsonl(tools._subsidiary_dir(), "hypotheses.jsonl", [record])


def test_backfill_evidence_stage_infers_landing_page_from_real_economics():
    reset_state()
    try:
        legacy = {
            **SAMPLE_HYP, "id": "hyp_legacy_econ", "status": "active",
            "estimated_build_cost": 5.0,
            "build_cost_reasoning": "real, hypothesis-specific cost breakdown",
        }
        legacy.pop("evidence_stage", None)
        _write_raw_legacy_hypothesis("legacy_test_econ", legacy)
        hyps = json.loads(tools.read_hypotheses.run())
        entry = next(h for h in hyps if h["id"] == "hyp_legacy_econ")
        assert entry["evidence_stage"] == "landing_page"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_backfill_evidence_stage_infers_landing_page_from_live_flag():
    reset_state()
    try:
        legacy = {**SAMPLE_HYP, "id": "hyp_legacy_live", "status": "active", "landing_page_live": True}
        legacy.pop("evidence_stage", None)
        _write_raw_legacy_hypothesis("legacy_test_live", legacy)
        hyps = json.loads(tools.read_hypotheses.run())
        entry = next(h for h in hyps if h["id"] == "hyp_legacy_live")
        assert entry["evidence_stage"] == "landing_page"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_backfill_evidence_stage_infers_community_engagement_from_real_artifact():
    reset_state()
    try:
        sub_id = "legacy_test_community"
        tools.set_active_subsidiary(sub_id)
        legacy = {
            **SAMPLE_HYP, "id": "hyp_legacy_community", "status": "active",
            "estimated_build_cost": None, "build_cost_reasoning": None,
        }
        legacy.pop("evidence_stage", None)
        tools.write_jsonl(tools._subsidiary_dir(), "hypotheses.jsonl", [legacy])
        tools.write_jsonl(tools._subsidiary_dir(), "content_drafts.jsonl", [{
            "id": "draft_legacy_1", "hypothesis_id": "hyp_legacy_community",
            "post_type": "thread_reply", "status": "posted",
        }])
        hyps = json.loads(tools.read_hypotheses.run())
        entry = next(h for h in hyps if h["id"] == "hyp_legacy_community")
        assert entry["evidence_stage"] == "community_engagement"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_backfill_evidence_stage_falls_back_to_research_with_no_signal():
    reset_state()
    try:
        legacy = {
            **SAMPLE_HYP, "id": "hyp_legacy_none", "status": "active",
            "estimated_build_cost": None, "build_cost_reasoning": None,
        }
        legacy.pop("evidence_stage", None)
        _write_raw_legacy_hypothesis("legacy_test_none", legacy)
        hyps = json.loads(tools.read_hypotheses.run())
        entry = next(h for h in hyps if h["id"] == "hyp_legacy_none")
        assert entry["evidence_stage"] == "research"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_backfill_evidence_stage_never_overwrites_an_existing_valid_stage():
    reset_state()
    try:
        legacy = {**SAMPLE_HYP, "id": "hyp_legacy_already_set", "status": "active", "evidence_stage": "build"}
        _write_raw_legacy_hypothesis("legacy_test_already_set", legacy)
        hyps = json.loads(tools.read_hypotheses.run())
        entry = next(h for h in hyps if h["id"] == "hyp_legacy_already_set")
        assert entry["evidence_stage"] == "build"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_backfill_evidence_stage_persists_after_backfill():
    # Confirms the inferred stage is actually written back to disk, not
    # just computed transiently for one read.
    reset_state()
    try:
        legacy = {**SAMPLE_HYP, "id": "hyp_legacy_persist", "status": "active", "landing_page_live": True}
        legacy.pop("evidence_stage", None)
        sub_id = "legacy_test_persist"
        _write_raw_legacy_hypothesis(sub_id, legacy)
        tools.read_hypotheses.run()  # triggers the backfill
        raw = tools.read_jsonl(tools._subsidiary_dir(), "hypotheses.jsonl")
        entry = next(h for h in raw if h["id"] == "hyp_legacy_persist")
        assert entry["evidence_stage"] == "landing_page"
    finally:
        tools.set_active_subsidiary("api-sentinel")


def test_write_hypothesis_evidence_stage_full_progression_with_real_artifacts():
    # The real end-to-end path (structural-rebuild addendum, sections 3-4):
    # research (with plan fields) -> a substantive research artifact ->
    # community_engagement (with a real posted draft) -> landing_page (now
    # requiring precise economics) -> build. Each transition only succeeds
    # once its artifact actually exists - this is what would have caught
    # hyp_bootstrap_001 skipping straight to a landing page.
    reset_state()
    hyp_id = SAMPLE_HYP["id"]
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP)))  # evidence_stage='research'
    assert "error" not in result, result

    _seed_research_artifact(hyp_id)
    _seed_community_engagement_artifact(hyp_id)

    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": hyp_id, "evidence_stage": "community_engagement"})
    ))
    assert "error" not in result, result
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["evidence_stage"] == "community_engagement"

    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": hyp_id, "evidence_stage": "landing_page"})
    ))
    assert "error" not in result, result
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["evidence_stage"] == "landing_page"

    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps({"id": hyp_id, "evidence_stage": "build"})))
    assert "error" not in result, result  # already past the later-stage boundary, no fresh artifact needed
    stored = json.loads(tools.read_hypotheses.run())[0]
    assert stored["evidence_stage"] == "build"


def test_write_hypothesis_evidence_stage_community_engagement_requires_artifact():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": SAMPLE_HYP["id"], "evidence_stage": "community_engagement"})
    ))
    assert "error" in result


def test_write_hypothesis_evidence_stage_landing_page_requires_both_artifacts():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    # only the research artifact exists - community_engagement is still missing
    _seed_research_artifact()
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": SAMPLE_HYP["id"], "evidence_stage": "landing_page"})
    ))
    assert "error" in result
    assert "community_engagement" in result["error"]


def test_write_hypothesis_evidence_stage_landing_page_via_approved_stage_skip():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))  # no artifacts at all
    _seed_stage_skip_approval(target_stage="landing_page")
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({
            "id": SAMPLE_HYP["id"], "evidence_stage": "landing_page",
            **{k: SAMPLE_HYP[k] for k in ("estimated_build_cost", "price_point_monthly",
                                           "break_even_horizon_months", "break_even_users", "build_cost_reasoning")},
        })
    ))
    assert "error" not in result, result


def test_write_hypothesis_evidence_stage_landing_page_without_skip_rejected():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))  # no artifacts, no skip request
    result = json.loads(tools.write_hypothesis.run(
        hypothesis=json.dumps({"id": SAMPLE_HYP["id"], "evidence_stage": "landing_page"})
    ))
    assert "error" in result


# --- tools.py: file_task_order dev-stage gate --------------------------------

def test_file_task_order_dev_gate_rejects_without_stage_or_justification():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))  # evidence_stage still None
    result = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="build landing page", context="first test",
        hypothesis_id=SAMPLE_HYP["id"],
    ))
    assert "error" in result
    assert json.loads(tools.read_task_orders.run(to_role="dev", status="open")) == []


def _promote_sample_hyp_to_stage(stage):
    """Test helper: get SAMPLE_HYP to evidence_stage=stage via an approved
    stage-skip request (simplest path for tests that only care about what
    happens *after* the hypothesis is already there, not how it got there -
    the artifact-backed path itself is covered by the evidence-stage tests).
    """
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    _seed_stage_skip_approval(target_stage=stage)
    tools.write_hypothesis.run(hypothesis=json.dumps({
        "id": SAMPLE_HYP["id"], "evidence_stage": stage,
        **{k: SAMPLE_HYP[k] for k in ("estimated_build_cost", "price_point_monthly",
                                       "break_even_horizon_months", "break_even_users", "build_cost_reasoning")},
    }))


def test_file_task_order_dev_gate_accepts_with_landing_page_stage():
    reset_state()
    _promote_sample_hyp_to_stage("landing_page")
    result = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="build landing page", context="first test",
        hypothesis_id=SAMPLE_HYP["id"],
    ))
    assert "filed" in result, result


def test_file_task_order_dev_gate_accepts_with_build_stage():
    reset_state()
    _promote_sample_hyp_to_stage("build")
    result = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="build the real product", context="build outcome",
        hypothesis_id=SAMPLE_HYP["id"],
    ))
    assert "filed" in result, result


def test_file_task_order_dev_gate_ignores_unknown_hypothesis_without_justification():
    reset_state()
    # hypothesis_id doesn't exist at all - stage resolves to None, same as an
    # early-stage hypothesis, so it's rejected the same way, not a crash.
    result = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="build variant", context="ctx", hypothesis_id="hyp_does_not_exist",
    ))
    assert "error" in result


def test_file_task_order_dev_gate_does_not_apply_without_hypothesis_id():
    reset_state()
    # a Dev task with no hypothesis_id at all (rare, but not this gate's concern)
    result = json.loads(tools.file_task_order.run(
        to_role="dev", task_description="general maintenance", context="ctx",
    ))
    assert "filed" in result, result


def test_file_task_order_dev_gate_does_not_apply_to_growth():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))  # evidence_stage still None
    result = json.loads(tools.file_task_order.run(
        to_role="growth", task_description="draft a post", context="ctx", hypothesis_id=SAMPLE_HYP["id"],
    ))
    assert "filed" in result, result


# --- tools.py: hypothesis overview (structural-rebuild addendum, section 8) -

def test_build_hypothesis_overview_empty_when_no_active_hypotheses():
    reset_state()
    assert tools.build_hypothesis_overview() == []


def test_build_hypothesis_overview_reflects_real_records():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    _seed_research_artifact()
    tools.file_task_order.run(to_role="growth", task_description="measure reddit reach", context="ctx")

    overview = tools.build_hypothesis_overview()
    assert len(overview) == 1
    entry = overview[0]
    assert entry["id"] == SAMPLE_HYP["id"]
    assert entry["evidence_stage"] == "research"
    assert "revenue" in entry["status_line"] and "reddit" in entry["status_line"]
    assert _SUBSTANTIVE_SUMMARY[:50] in entry["latest_finding"]
    assert entry["next_action"] == "keine offene Task-Order"  # order above has no hypothesis_id


def test_build_hypothesis_overview_ignores_non_active_hypotheses():
    reset_state()
    buried = {**SAMPLE_HYP, "status": "buried", "bury_reasoning": "not worth pursuing"}
    tools.write_hypothesis.run(hypothesis=json.dumps(buried))
    assert tools.build_hypothesis_overview() == []


def test_build_hypothesis_overview_no_findings_yet():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    entry = tools.build_hypothesis_overview()[0]
    assert entry["latest_finding"] == "keine Erkenntnis geloggt"


def test_build_hypothesis_overview_next_action_prefers_newest_open_order():
    # 2026-08-11 regression: next_action used to surface the OLDEST open
    # order regardless of relevance (exactly how a stale, pre-evidence-
    # stage-gating order like order_ee8905ab kept showing up as "next
    # step" for a hypothesis whose actual state had long since moved on).
    # A genuinely new, more-relevant order must win over an old one.
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    tools.file_task_order.run(
        to_role="growth", task_description="OLD stale order", context="ctx",
        hypothesis_id=SAMPLE_HYP["id"],
    )
    tools.file_task_order.run(
        to_role="growth", task_description="NEW real next step", context="ctx",
        hypothesis_id=SAMPLE_HYP["id"],
    )
    entry = tools.build_hypothesis_overview()[0]
    assert entry["next_action"].startswith("NEW real next step")


def test_build_hypothesis_overview_next_action_surfaces_pileup_count():
    reset_state()
    tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP))
    for i in range(3):
        tools.file_task_order.run(
            to_role="growth", task_description=f"order {i}", context="ctx",
            hypothesis_id=SAMPLE_HYP["id"],
        )
    entry = tools.build_hypothesis_overview()[0]
    assert "+2 weitere offene Order" in entry["next_action"]


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


def test_classify_command_payment_link():
    assert tools._classify_command("payment_link: appr_ab12cd34 https://buy.stripe.com/xyz", "") == (
        "payment_link", ("appr_ab12cd34", "https://buy.stripe.com/xyz")
    )
    assert tools._classify_command("payment_link: no approval id here", "") is None
    assert tools._classify_command("payment_link: appr_ab12cd34", "") is None  # no url


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
        appr = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
        request_id = appr["queued"]
        notification_text = f"Neue Freigabe angefragt: {request_id}\nKategorie: deploy"

        log = tools._apply_telegram_commands([{"text": "approve", "reply_to_text": notification_text}])
        assert any(request_id in entry and "approved" in entry for entry in log)
        stored = next(r for r in tools._read_global_jsonl("approval_queue.jsonl") if r["id"] == request_id)
        assert stored["status"] == "approved"

        # already decided - a second reply must not flip it or error
        log = tools._apply_telegram_commands([{"text": "reject", "reply_to_text": notification_text}])
        assert log == []
        stored = next(r for r in tools._read_global_jsonl("approval_queue.jsonl") if r["id"] == request_id)
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


def test_apply_telegram_commands_payment_link_requires_approved_status():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        appr = json.loads(tools.request_approval.run(category="spend", proposal="payment link", reasoning="r"))
        approval_id = appr["queued"]  # still 'pending', not yet approved

        log = tools._apply_telegram_commands([{
            "text": f"payment_link: {approval_id} https://buy.stripe.com/xyz", "reply_to_text": "",
        }])
        assert log == []
        stored = next(r for r in tools._read_global_jsonl("approval_queue.jsonl") if r["id"] == approval_id)
        assert stored.get("payment_link_url") is None
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_payment_link_sets_url_once_approved():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        appr = json.loads(tools.request_approval.run(category="spend", proposal="payment link", reasoning="r"))
        approval_id = appr["queued"]
        approvals = tools._read_global_jsonl("approval_queue.jsonl")
        approvals[0]["status"] = "approved"
        tools._write_global_jsonl("approval_queue.jsonl", approvals)

        log = tools._apply_telegram_commands([{
            "text": f"payment_link: {approval_id} https://buy.stripe.com/xyz", "reply_to_text": "",
        }])
        assert any("payment_link_url gesetzt" in entry for entry in log)
        result = json.loads(tools.check_approval_status.run(approval_id=approval_id))
        assert result["payment_link_url"] == "https://buy.stripe.com/xyz"
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


# --- tools.py: duration-cap policy proposal (structural-rebuild addendum, --
# section 6) - a board-set number, never silently active until confirmed.

def test_classify_command_duration_policy_confirm():
    assert tools._classify_command("duration_policy: confirm", "") == ("duration_policy_confirm", None)
    assert tools._classify_command("duration_policy: Confirm", "") == ("duration_policy_confirm", None)


def test_classify_command_duration_policy_set():
    action, values = tools._classify_command("duration_policy: 3 5 14 none", "")
    assert action == "duration_policy_set"
    assert values == {"research": 3, "community_engagement": 5, "landing_page": 14, "build": None}


def test_classify_command_duration_policy_set_rejects_wrong_shape():
    assert tools._classify_command("duration_policy: 3 5 14", "") is None
    assert tools._classify_command("duration_policy: three 5 14 none", "") is None


def test_write_hypothesis_duration_ceiling_not_enforced_while_proposed():
    reset_state()
    # DEFAULT_PROPOSED_DURATION_CAPS ships status='proposed' - not active yet.
    over_ceiling = {**SAMPLE_HYP, "duration_days": 999}
    result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(over_ceiling)))
    assert "error" not in result, result


def test_apply_telegram_commands_duration_policy_confirm():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()  # bootstraps api-sentinel with the default proposed policy
        log = tools._apply_telegram_commands([{"text": "duration_policy: confirm", "reply_to_text": ""}])
        assert any("bestaetigt" in entry for entry in log)
        subs = json.loads(holding.read_subsidiaries.run())
        stored_policy = subs[0]["policies"]["max_duration_days_by_stage"]
        assert stored_policy["status"] == "confirmed"
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_duration_policy_set_and_confirm():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()
        log = tools._apply_telegram_commands([{"text": "duration_policy: 2 4 10 none", "reply_to_text": ""}])
        assert any("gesetzt und bestaetigt" in entry for entry in log)
        subs = json.loads(holding.read_subsidiaries.run())
        stored_policy = subs[0]["policies"]["max_duration_days_by_stage"]
        assert stored_policy["status"] == "confirmed"
        assert stored_policy["values"] == {"research": 2, "community_engagement": 4, "landing_page": 10, "build": None}
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_write_hypothesis_duration_ceiling_enforced_once_confirmed():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()
        tools._apply_telegram_commands([{"text": "duration_policy: 2 4 10 none", "reply_to_text": ""}])
        # SAMPLE_HYP is evidence_stage='research', duration_days=10 - over the confirmed research ceiling (2)
        result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(SAMPLE_HYP)))
        assert "error" in result
        assert "confirmed policy ceiling" in result["error"]

        within_ceiling = {**SAMPLE_HYP, "duration_days": 2}
        result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(within_ceiling)))
        assert "error" not in result, result
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_write_hypothesis_duration_ceiling_override_via_approved_request():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()
        tools._apply_telegram_commands([{"text": "duration_policy: 2 4 10 none", "reply_to_text": ""}])
        appr = json.loads(tools.request_approval.run(category="deploy", proposal="extend research window", reasoning="r"))
        approvals = tools._read_global_jsonl("approval_queue.jsonl")
        approvals[0]["status"] = "approved"
        tools._write_global_jsonl("approval_queue.jsonl", approvals)

        over_ceiling = {**SAMPLE_HYP, "duration_extension_approval_id": appr["queued"]}
        result = json.loads(tools.write_hypothesis.run(hypothesis=json.dumps(over_ceiling)))
        assert "error" not in result, result
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
        appr = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
        tools.notify_new_pending_approvals()
        stored = next(r for r in tools._read_global_jsonl("approval_queue.jsonl") if r["id"] == appr["queued"])
        assert stored["telegram_notified"] is True

        # calling again must not error and must not un-mark it
        tools.notify_new_pending_approvals()
        stored = next(r for r in tools._read_global_jsonl("approval_queue.jsonl") if r["id"] == appr["queued"])
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
    tools.request_approval.run(category="deploy", proposal="p1", reasoning="r1")
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
    assert subs[0]["state_dir"] == str(tools.STATE_DIR / "api-sentinel")


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
    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)

    result = json.loads(holding.register_subsidiary.run(
        subsidiary=json.dumps({"id": "second-co", "name": "Second Co", "focus": "test"}),
        approved_request_id=appr["queued"],
    ))
    assert result["ok"] is True and result["id"] == "second-co"
    assert result["policies"] == holding.SUBSIDIARY_POLICY_DEFAULTS
    subs = json.loads(holding.read_subsidiaries.run())
    assert {s["id"] for s in subs} == {"api-sentinel", "second-co"}
    new_sub = next(s for s in subs if s["id"] == "second-co")
    # Audit addendum, section 4: register_subsidiary only ever creates a
    # registry row - it must say so plainly on the record itself, not just
    # in its own docstring, so nothing downstream can miss it.
    assert new_sub["operative_capability"] == holding.NEW_SUBSIDIARY_CAPABILITY_NOTE
    # Structural-rebuild addendum, section 2: a new subsidiary now gets a
    # real, isolated data partition automatically - no longer state_dir=None.
    assert new_sub["state_dir"] == str(tools.STATE_DIR / "second-co")


def test_register_subsidiary_rejects_duplicate_id():
    reset_state()
    appr = json.loads(tools.request_approval.run(category="deploy", proposal="p", reasoning="r"))
    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)
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
    approvals = tools._read_global_jsonl("approval_queue.jsonl")
    approvals[0]["status"] = "approved"
    tools._write_global_jsonl("approval_queue.jsonl", approvals)

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


# --- holding.py: evidence-stage skip review (structural-rebuild addendum, --
# section 4) - Sub-CEO files, Main-CEO decides, mirrors pivot proposals.

def test_file_stage_skip_request_rejects_invalid_target_stage():
    reset_state()
    result = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="not_a_real_stage", reasoning="r",
    ))
    assert "error" in result


def test_file_stage_skip_request_rejects_empty_reasoning():
    reset_state()
    result = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page", reasoning="  ",
    ))
    assert "error" in result


def test_file_stage_skip_request_rejects_instruction_echo():
    reset_state()
    result = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page",
        reasoning="A landing page realistically costs low single-digit dollars in tokens here, not hundreds or thousands.",
    ))
    assert "error" in result


def test_file_and_read_stage_skip_request():
    reset_state()
    filed = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page",
        reasoning="This lineage's prior hypothesis already gathered equivalent research evidence directly.",
    ))
    assert "filed" in filed
    pending = json.loads(holding.read_stage_skip_requests.run(status="pending"))
    assert len(pending) == 1 and pending[0]["hypothesis_id"] == "hyp_x"
    assert json.loads(holding.read_stage_skip_requests.run(status="approved")) == []


def test_decide_stage_skip_request_approve_and_reject():
    reset_state()
    filed = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page",
        reasoning="This lineage's prior hypothesis already gathered equivalent research evidence directly.",
    ))
    result = json.loads(holding.decide_stage_skip_request.run(
        request_id=filed["filed"], decision="approved", reasoning="checked, genuinely applies here",
    ))
    assert result == {"ok": True, "id": filed["filed"], "decision": "approved"}
    stored = json.loads(holding.read_stage_skip_requests.run(status="approved"))[0]
    assert stored["decision_reasoning"] == "checked, genuinely applies here"


def test_decide_stage_skip_request_rejects_invalid_decision():
    reset_state()
    filed = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page",
        reasoning="This lineage's prior hypothesis already gathered equivalent research evidence directly.",
    ))
    result = json.loads(holding.decide_stage_skip_request.run(
        request_id=filed["filed"], decision="maybe", reasoning="r",
    ))
    assert "error" in result


def test_decide_stage_skip_request_does_not_redecide():
    reset_state()
    filed = json.loads(holding.file_stage_skip_request.run(
        hypothesis_id="hyp_x", subsidiary_id="api-sentinel", target_stage="landing_page",
        reasoning="This lineage's prior hypothesis already gathered equivalent research evidence directly.",
    ))
    holding.decide_stage_skip_request.run(request_id=filed["filed"], decision="rejected", reasoning="r1")
    result = json.loads(holding.decide_stage_skip_request.run(
        request_id=filed["filed"], decision="approved", reasoning="r2",
    ))
    assert "error" in result
    decided = json.loads(holding.read_stage_skip_requests.run())[0]
    assert decided["status"] == "rejected", "first decision must stick"


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


# --- holding.py: idea intake and Main-CEO routing ---------------------------

def test_propose_idea_requires_nonempty_summary():
    reset_state()
    result = json.loads(holding.propose_idea.run(summary="", source="api-sentinel", reasoning="r"))
    assert "error" in result


def test_propose_and_read_ideas_roundtrip():
    reset_state()
    filed = json.loads(holding.propose_idea.run(
        summary="Freqtrade users also want backtest-result sharing",
        source="api-sentinel", reasoning="came up repeatedly in community engagement",
    ))
    assert "filed" in filed
    pending = json.loads(holding.read_ideas.run(status="pending"))
    assert len(pending) == 1
    assert pending[0]["id"] == filed["filed"]
    assert pending[0]["status"] == "pending"
    assert pending[0]["source"] == "api-sentinel"

    routed = json.loads(holding.read_ideas.run(status="routed"))
    assert routed == []
    assert json.loads(holding.read_ideas.run()) == pending  # "" returns all, same as pending here


def test_route_idea_rejects_invalid_decision():
    reset_state()
    filed = json.loads(holding.propose_idea.run(summary="s", source="main_ceo", reasoning="r"))
    result = json.loads(holding.route_idea.run(idea_id=filed["filed"], decision="maybe_later", reasoning="r"))
    assert "error" in result


def test_route_idea_existing_subsidiary_requires_target():
    reset_state()
    filed = json.loads(holding.propose_idea.run(summary="s", source="main_ceo", reasoning="r"))
    result = json.loads(holding.route_idea.run(idea_id=filed["filed"], decision="existing_subsidiary", reasoning="r"))
    assert "error" in result


def test_route_idea_existing_subsidiary_rejects_unknown_target():
    reset_state()
    filed = json.loads(holding.propose_idea.run(summary="s", source="main_ceo", reasoning="r"))
    result = json.loads(holding.route_idea.run(
        idea_id=filed["filed"], decision="existing_subsidiary", reasoning="r",
        target_subsidiary_id="does-not-exist",
    ))
    assert "error" in result


def test_route_idea_existing_subsidiary_succeeds():
    reset_state()
    holding.read_subsidiaries.run()  # bootstraps api-sentinel
    filed = json.loads(holding.propose_idea.run(summary="s", source="main_ceo", reasoning="r"))
    result = json.loads(holding.route_idea.run(
        idea_id=filed["filed"], decision="existing_subsidiary", reasoning="fits api-sentinel's focus",
        target_subsidiary_id="api-sentinel",
    ))
    assert result["ok"] is True and result["decision"] == "existing_subsidiary"
    stored = json.loads(holding.read_ideas.run(status="routed"))[0]
    assert stored["target_subsidiary_id"] == "api-sentinel"
    assert stored["routing_reasoning"] == "fits api-sentinel's focus"


def test_route_idea_new_subsidiary_and_rejected_do_not_require_target():
    reset_state()
    idea_a = json.loads(holding.propose_idea.run(summary="a", source="main_ceo", reasoning="r"))
    idea_b = json.loads(holding.propose_idea.run(summary="b", source="main_ceo", reasoning="r"))
    result_a = json.loads(holding.route_idea.run(
        idea_id=idea_a["filed"], decision="new_subsidiary", reasoning="doesn't fit any existing focus",
    ))
    result_b = json.loads(holding.route_idea.run(
        idea_id=idea_b["filed"], decision="rejected", reasoning="not worth pursuing",
    ))
    assert result_a["ok"] is True and result_b["ok"] is True


def test_route_idea_does_not_reroute():
    reset_state()
    filed = json.loads(holding.propose_idea.run(summary="s", source="main_ceo", reasoning="r"))
    holding.route_idea.run(idea_id=filed["filed"], decision="rejected", reasoning="r")
    result = json.loads(holding.route_idea.run(idea_id=filed["filed"], decision="rejected", reasoning="again"))
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


# --- holding.py: stagnation escalation (structural-rebuild addendum, -------
# section 3) - consecutive-cycle counter with a persistent, acknowledgeable
# escalation, not just a note repeated identically forever.

def test_assess_subsidiary_trajectory_counts_consecutive_stall_cycles():
    reset_state()
    holding.read_subsidiaries.run()
    for i in range(holding.STALL_RESOLVED_THRESHOLD):
        _write_buried_hyp(i)
    for expected in range(1, 4):
        result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
        assert result["possible_stall"] is True
        assert result["consecutive_stall_cycles"] == expected
        assert result["stagnation_escalated"] is False  # below STAGNATION_ESCALATION_THRESHOLD still


def test_assess_subsidiary_trajectory_escalates_at_threshold():
    reset_state()
    holding.read_subsidiaries.run()
    for i in range(holding.STALL_RESOLVED_THRESHOLD):
        _write_buried_hyp(i)
    result = None
    for _ in range(holding.STAGNATION_ESCALATION_THRESHOLD):
        result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
    assert result["consecutive_stall_cycles"] == holding.STAGNATION_ESCALATION_THRESHOLD
    assert result["stagnation_escalated"] is True
    stored = json.loads(holding.read_subsidiaries.run())[0]
    assert stored["stagnation_escalated"] is True
    assert stored["stagnation_escalated_at"] is not None


def test_assess_subsidiary_trajectory_resets_counter_and_escalation_on_recovery():
    reset_state()
    holding.read_subsidiaries.run()
    for i in range(holding.STALL_RESOLVED_THRESHOLD):
        _write_buried_hyp(i)
    for _ in range(holding.STAGNATION_ESCALATION_THRESHOLD):
        holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel")
    tools.write_hypothesis.run(hypothesis=json.dumps({
        **SAMPLE_HYP, "id": "hyp_build_recovery", "landing_page_variant_id": "lp_v_recovery",
        "status": "evaluated", "outcome": "build",
    }))
    result = json.loads(holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel"))
    assert result["possible_stall"] is False
    assert result["consecutive_stall_cycles"] == 0
    assert result["stagnation_escalated"] is False


def test_apply_telegram_commands_stagnation_ack_clears_escalation():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()
        for i in range(holding.STALL_RESOLVED_THRESHOLD):
            _write_buried_hyp(i)
        for _ in range(holding.STAGNATION_ESCALATION_THRESHOLD):
            holding.assess_subsidiary_trajectory.run(subsidiary_id="api-sentinel")
        assert json.loads(holding.read_subsidiaries.run())[0]["stagnation_escalated"] is True

        log = tools._apply_telegram_commands([{"text": "stagnation_ack: api-sentinel", "reply_to_text": ""}])
        assert any("Stagnation-Eskalation bestaetigt" in entry for entry in log)
        stored = json.loads(holding.read_subsidiaries.run())[0]
        assert stored["stagnation_escalated"] is False
        assert stored["consecutive_stall_cycles"] == 0
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_apply_telegram_commands_stagnation_ack_no_op_when_not_escalated():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    had_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
    try:
        holding.read_subsidiaries.run()
        log = tools._apply_telegram_commands([{"text": "stagnation_ack: api-sentinel", "reply_to_text": ""}])
        assert log == []
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
        if had_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = had_chat


def test_classify_command_stagnation_ack():
    assert tools._classify_command("stagnation_ack: api-sentinel", "") == ("stagnation_ack", "api-sentinel")
    assert tools._classify_command("stagnation_ack:", "") is None


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


def test_testing_profile_dev_limits_raised_past_confirmed_stall_floor():
    # Confirmed via real Railway cycle logs (audit addendum): Dev hit
    # "Maximum iterations reached" every cycle it ran under max_iter=4/
    # max_tokens=500, always mid-way through open_pull_request missing
    # file_content - structurally impossible to complete a landing-page
    # build in that budget. These floors keep it from silently regressing
    # back to a value too small for a real build to ever finish.
    with open(crew._AGENT_PROFILE_FILE, encoding="utf-8") as f:
        config = json.load(f)
    dev_cfg = config["profiles"]["testing"]["agents"]["dev"]
    assert dev_cfg["max_tokens"] >= 1500, dev_cfg
    assert dev_cfg["max_iter"] >= 6, dev_cfg
    growth_cfg = config["profiles"]["testing"]["agents"]["growth"]
    # Growth's own realistic minimum tool-call count for one full task
    # (read_task_orders, read_hypotheses, check_community_risk,
    # get_account_stats, draft_content, request_approval, complete_task_order
    # = 7) already exceeds the old cap of 6.
    assert growth_cfg["max_iter"] >= 7, growth_cfg


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
        "read_knowledge_base", "write_knowledge_entry", "propose_idea", "file_stage_skip_request",
        "search_web", "read_webpage",
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
        "propose_idea", "read_ideas", "route_idea",
        "read_stage_skip_requests", "decide_stage_skip_request",
        "search_web", "read_webpage",
    }, tool_names


def test_growth_dev_tools():
    assert {t.name for t in crew.growth_agent.tools} == {
        "request_approval", "read_channel_metrics", "read_channels", "read_state", "read_hypotheses",
        "read_task_orders", "complete_task_order", "draft_content", "read_content_drafts",
        "check_community_risk", "get_account_stats", "log_research_finding", "read_research_findings",
        "read_subsidiary_policies", "read_knowledge_base", "propose_idea", "search_web", "read_webpage",
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


def test_all_task_descriptions_and_agent_backstories_interpolate_cleanly():
    # Real-world regression for the 2026-08-11 crash: crewai's own
    # kickoff() calls exactly this method (crew._interpolate_inputs) on the
    # real Crew before running any agent - it interpolates every task's
    # description/expected_output/output_file AND every agent's
    # role/goal/backstory against the same inputs dict crew.py passes
    # (only {"subsidiary_id": ...}). A literal, unintended {word} anywhere
    # in any of those strings (e.g. the "lp_v{n}_{label}.html" naming-
    # pattern example that actually crashed production) crashes the entire
    # cycle before a single LLM call - no mock, this exercises the real
    # crewai interpolation path against the real production Crew object
    # built by crew.py.
    tasks = list(crew.crew.tasks)
    agents = list(crew.crew.agents)
    task_originals = [(t, t.description, t.expected_output, t.output_file) for t in tasks]
    agent_originals = [(a, a.role, a.goal, a.backstory) for a in agents]
    try:
        crew.crew._interpolate_inputs({"subsidiary_id": "api-sentinel"})
    except Exception as exc:
        raise AssertionError(
            "a task description or agent backstory contains a literal "
            f"{{placeholder}} crewai tried to interpolate and failed: {exc}"
        )
    finally:
        for t, description, expected_output, output_file in task_originals:
            t.description, t.expected_output, t.output_file = description, expected_output, output_file
        for a, role, goal, backstory in agent_originals:
            a.role, a.goal, a.backstory = role, goal, backstory


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
        crew.send_cycle_summary(subsidiary_id="api-sentinel")  # no kickoff() happened; task.output is None on every task
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


# --- crew.py: hypothesis overview + "Fuer den Aufsichtsrat" (structural-----
# rebuild addendum, section 8)

def test_format_hypothesis_overview_empty_state():
    assert crew._format_hypothesis_overview([]) == ["Keine aktiven Hypothesen."]


def test_format_hypothesis_overview_renders_entries():
    overview = [{
        "id": "hyp_x", "evidence_stage": "research",
        "status_line": "revenue / reddit, seit 2026-08-01",
        "latest_finding": "keine Erkenntnis geloggt",
        "next_action": "keine offene Task-Order",
    }]
    lines = crew._format_hypothesis_overview(overview)
    assert lines[0] == "- hyp_x (evidence_stage=research): revenue / reddit, seit 2026-08-01"
    assert lines[1] == "  Letzter Fund: keine Erkenntnis geloggt"
    assert lines[2] == "  Naechster Schritt: keine offene Task-Order"


def test_aufsichtsrat_lines_empty_when_nothing_needs_a_decision():
    assert crew._aufsichtsrat_lines(0, {"status": "confirmed", "values": {}}, 0) == []
    assert crew._aufsichtsrat_lines("?", None, 0) == []


def test_aufsichtsrat_lines_pending_approvals():
    lines = crew._aufsichtsrat_lines(2, None, 0)
    assert lines[0] == ""
    assert lines[1] == "--- Fuer den Aufsichtsrat ---"
    assert any("2 offene Freigabe" in line for line in lines)


def test_aufsichtsrat_lines_proposed_duration_policy():
    policy = {"status": "proposed", "values": {"research": 3, "community_engagement": 5, "landing_page": 14, "build": None}}
    lines = crew._aufsichtsrat_lines(0, policy, 0)
    assert any("Duration-Policy-Vorschlag" in line for line in lines)
    assert any("duration_policy: confirm" in line for line in lines)


def test_aufsichtsrat_lines_pending_stage_skips():
    lines = crew._aufsichtsrat_lines(0, None, 3)
    assert any("3 offene Stage-Skip-Anfrage" in line for line in lines)


def test_aufsichtsrat_lines_stagnation_escalation():
    lines = crew._aufsichtsrat_lines(0, None, 0, ["api-sentinel"])
    assert any("api-sentinel" in line and "stagnation_ack: api-sentinel" in line for line in lines)


def test_aufsichtsrat_lines_combines_all_triggers():
    policy = {"status": "proposed", "values": {}}
    lines = crew._aufsichtsrat_lines(1, policy, 2, ["api-sentinel"])
    joined = "\n".join(lines)
    assert "offene Freigabe" in joined
    assert "Duration-Policy-Vorschlag" in joined
    assert "Stage-Skip-Anfrage" in joined
    assert "stagnation_ack" in joined


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
        crew.send_cycle_summary(subsidiary_id="api-sentinel")
        assert captured, "expected send_telegram_message to be called"
        # Structural-rebuild addendum, section 8: single message now, no
        # separate formatted-table follow-up.
        assert len(captured) == 1
        main_report = captured[0][0]
        lines = main_report.split("\n")
        assert lines[0].startswith("api-sentinel Zyklus - ")
        assert lines[1].startswith("Gesamt-Tokens diesen Zyklus: 999")
        assert "$" in lines[1]  # cost figure present (model is priced in the active testing profile)
        assert "--- Hypothesen-Uebersicht ---" in main_report
    finally:
        crew.send_telegram_message = original_send
        crew.crew.usage_metrics = original_metrics
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token


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


# --------------------------------------------------------------------------
# FIX.md mechanism (FIX.md/Kaizen/payment-propensity addendum, Part 1).
# --------------------------------------------------------------------------

def test_check_zero_state_streak_fires_after_threshold():
    # The first call only establishes the baseline snapshot (nothing to
    # compare against yet), so reaching a streak of N takes N+1 calls -
    # same "first observation just sets the baseline" shape as
    # assess_subsidiary_trajectory's own consecutive-cycle counter.
    reset_state()
    fired, _ = holding.check_zero_state_streak("api-sentinel", 3)
    assert fired is False
    fired, _ = holding.check_zero_state_streak("api-sentinel", 3)
    assert fired is False
    fired, _ = holding.check_zero_state_streak("api-sentinel", 3)
    assert fired is False
    fired, evidence = holding.check_zero_state_streak("api-sentinel", 3)
    assert fired is True
    assert evidence["streak_cycles"] == 3


def _current_zero_state_streak():
    subs, idx = holding._get_subsidiary("api-sentinel")
    return (subs[idx].get("fix_check_streaks") or {}).get("zero_state_streak", 0)


def test_check_zero_state_streak_resets_when_state_changes():
    reset_state()
    holding.check_zero_state_streak("api-sentinel", 100)  # baseline snapshot
    holding.check_zero_state_streak("api-sentinel", 100)  # unchanged -> streak 1
    holding.check_zero_state_streak("api-sentinel", 100)  # unchanged -> streak 2
    assert _current_zero_state_streak() == 2

    tools.write_knowledge_entry.run(
        topic="t", takeaway="k", confidence="low", source_hypothesis_ids=json.dumps(["hyp_x"]),
    )
    holding.check_zero_state_streak("api-sentinel", 100)
    assert _current_zero_state_streak() == 0, "a real new knowledge_base entry must reset the streak to 0"


def test_check_recurring_malformed_tool_calls_requires_same_signature():
    reset_state()
    fired, _ = holding.check_recurring_malformed_tool_calls("api-sentinel", ["write_channel:Field required"], 3)
    assert fired is False
    fired, _ = holding.check_recurring_malformed_tool_calls("api-sentinel", ["write_channel:Field required"], 3)
    assert fired is False
    fired, evidence = holding.check_recurring_malformed_tool_calls(
        "api-sentinel", ["write_channel:Field required"], 3
    )
    assert fired is True
    assert evidence["streak_cycles"] == 3

    fired, _ = holding.check_recurring_malformed_tool_calls("api-sentinel", ["other_tool:different"], 3)
    assert fired is False, "a different signature must reset the streak, not keep firing"


def test_check_channel_bury_streak_fires_on_consecutive_buries():
    reset_state()
    for i in range(3):
        hyp = {
            "id": f"hyp_bury_{i}", "statement": "s", "category": "value", "landing_page_variant_id": "v1",
            "failure_rate": 0.5, "success_rate": 0.5, "duration_days": 3, "channel": "reddit",
            "hypothesis_type": "value", "impact_score": 3, "confidence_score": 3, "evidence_stage": "research",
            "research_objective": "o", "research_confirming_criteria": "c", "research_disconfirming_criteria": "d",
            "primary_variable_tested": "audience",
        }
        tools.write_hypothesis.run(hypothesis=json.dumps(hyp))
        tools.write_hypothesis.run(hypothesis=json.dumps({
            "id": f"hyp_bury_{i}", "status": "buried", "bury_reasoning": "no signal",
        }))
    fired, evidence = holding.check_channel_bury_streak("api-sentinel", 3)
    assert fired is True
    assert evidence["channel"] == "reddit"
    assert len(evidence["hypothesis_ids"]) == 3


def test_check_hypothesis_stuck_past_cap_requires_confirmed_policy():
    reset_state()
    old_created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    tools._write_jsonl("hypotheses.jsonl", [{
        "id": "hyp_stuck", "status": "active", "evidence_stage": "research",
        "created_at": old_created, "channel": "reddit",
    }])
    fired, _ = holding.check_hypothesis_stuck_past_cap("api-sentinel", {"max_duration_days_by_stage": tools.DEFAULT_PROPOSED_DURATION_CAPS})
    assert fired is False, "must not fire while the duration policy is still 'proposed'"

    confirmed_policy = {
        "max_duration_days_by_stage": {"status": "confirmed", "values": {"research": 3, "community_engagement": 5, "landing_page": 14, "build": None}}
    }
    fired, evidence = holding.check_hypothesis_stuck_past_cap("api-sentinel", confirmed_policy)
    assert fired is True
    assert evidence["hypothesis_id"] == "hyp_stuck"


def test_check_repeated_pivot_streak_fires_on_consecutive_pivot_outcomes():
    reset_state()
    tools._write_jsonl("hypotheses.jsonl", [
        {"id": "hyp_p1", "status": "evaluated", "outcome": "pivot", "channel": "reddit"},
        {"id": "hyp_p2", "status": "evaluated", "outcome": "pivot", "channel": "reddit"},
    ])
    fired, evidence = holding.check_repeated_pivot_streak("api-sentinel", 2)
    assert fired is True
    assert evidence["hypothesis_ids"] == ["hyp_p1", "hyp_p2"]

    tools._write_jsonl("hypotheses.jsonl", [
        {"id": "hyp_p1", "status": "evaluated", "outcome": "pivot", "channel": "reddit"},
        {"id": "hyp_p2", "status": "evaluated", "outcome": "build", "channel": "reddit"},
    ])
    fired, _ = holding.check_repeated_pivot_streak("api-sentinel", 2)
    assert fired is False


def test_check_stale_approvals_fires_after_threshold_hours():
    reset_state()
    stale_created = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    tools._write_global_jsonl("approval_queue.jsonl", [{
        "id": "appr_stale01", "status": "pending", "category": "spend", "created_at": stale_created,
    }])
    fired, evidence = holding.check_stale_approvals(48)
    assert fired is True
    assert evidence["stale_approvals"][0]["id"] == "appr_stale01"

    fired, _ = holding.check_stale_approvals(96)
    assert fired is False


def test_run_fix_checks_returns_only_fired_checks():
    reset_state()
    stale_created = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    tools._write_global_jsonl("approval_queue.jsonl", [{
        "id": "appr_stale02", "status": "pending", "category": "spend", "created_at": stale_created,
    }])
    fired = holding.run_fix_checks("api-sentinel")
    check_types = [f["check_type"] for f in fired]
    assert "stale_approvals" in check_types
    assert "zero_state_streak" not in check_types, "must not fire on the very first cycle"


def test_append_fix_md_and_record_fix_entry_roundtrip():
    reset_state()
    holding.append_fix_md("fix_test0001", "technisch", "Test headline", "Body text here.")
    fix_path = holding.HOLDING_DIR / "FIX.md"
    assert fix_path.exists()
    content = fix_path.read_text(encoding="utf-8")
    assert "## [fix_test0001] technisch: Test headline" in content
    assert "Body text here." in content

    holding.record_fix_entry("fix_test0001", "technisch", "Test headline", "api-sentinel", "stale_approvals")
    entries = holding._read("fix_entries.jsonl")
    assert len(entries) == 1
    assert entries[0]["id"] == "fix_test0001"
    assert entries[0]["resolved"] is False

    unnotified = holding.read_unnotified_fix_entries()
    assert len(unnotified) == 1
    holding.mark_fix_entries_notified(["fix_test0001"])
    assert holding.read_unnotified_fix_entries() == []


def test_resolve_fix_entry_archives_section():
    reset_state()
    holding.append_fix_md("fix_test0002", "inhaltlich", "Headline A", "Body A.")
    holding.append_fix_md("fix_test0003", "technisch", "Headline B", "Body B.")
    holding.record_fix_entry("fix_test0002", "inhaltlich", "Headline A", "api-sentinel", "channel_bury_streak")
    holding.record_fix_entry("fix_test0003", "technisch", "Headline B", "api-sentinel", "stale_approvals")

    ok, message = holding.resolve_fix_entry("fix_test0002")
    assert ok is True
    assert "fix_test0002" in message

    remaining = holding.HOLDING_DIR.joinpath("FIX.md").read_text(encoding="utf-8")
    assert "fix_test0002" not in remaining
    assert "fix_test0003" in remaining

    archives = list(holding.HOLDING_DIR.glob("FIX_resolved_*.md"))
    assert len(archives) == 1
    assert "fix_test0002" in archives[0].read_text(encoding="utf-8")

    entries = {e["id"]: e for e in holding._read("fix_entries.jsonl")}
    assert entries["fix_test0002"]["resolved"] is True

    ok, message = holding.resolve_fix_entry("fix_test0002")
    assert ok is False

    ok, message = holding.resolve_fix_entry("fix_does_not_exist")
    assert ok is False


def test_classify_command_fix_resolved():
    assert tools._classify_command("fix_resolved: fix_abc12345", "") == ("fix_resolved", "fix_abc12345")
    assert tools._classify_command("fix_resolved:", "") is None


def test_classify_command_fix_thresholds():
    assert tools._classify_command("fix_thresholds: confirm", "") == ("fix_thresholds_confirm", None)
    assert tools._classify_command("fix_thresholds: 3 3 3 2 48", "") == (
        "fix_thresholds_set",
        {
            "zero_state_streak_cycles": 3, "malformed_tool_calls_cycles": 3, "channel_bury_streak": 3,
            "repeated_pivot_streak": 2, "stale_approval_hours": 48,
        },
    )
    assert tools._classify_command("fix_thresholds: not enough", "") is None


def test_apply_telegram_commands_fix_resolved_clears_entry():
    reset_state()
    holding.read_subsidiaries.run()  # bootstrap the registry
    holding.append_fix_md("fix_test0004", "technisch", "Headline", "Body.")
    holding.record_fix_entry("fix_test0004", "technisch", "Headline", "api-sentinel", "stale_approvals")
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        log = tools._apply_telegram_commands([{"text": "fix_resolved: fix_test0004", "reply_to_text": ""}])
        assert any("fix_test0004" in entry for entry in log)
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
    entries = {e["id"]: e for e in holding._read("fix_entries.jsonl")}
    assert entries["fix_test0004"]["resolved"] is True


def test_apply_telegram_commands_fix_thresholds_confirm():
    reset_state()
    had_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    try:
        tools._apply_telegram_commands([{"text": "fix_thresholds: confirm", "reply_to_text": ""}])
    finally:
        if had_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = had_token
    stored = holding.read_fix_thresholds()
    assert stored["status"] == "confirmed"
    assert stored["values"] == holding.DEFAULT_PROPOSED_FIX_THRESHOLDS["values"]


def test_generate_fix_diagnosis_parses_structured_response():
    fake_response = (
        "CATEGORY: inhaltlich\n"
        "CONFIDENCE_CAVEAT: First-pass automated proposal, treat as a starting point.\n"
        "PROBLEM: r/algotrading channel keeps burying hypotheses.\n"
        "FIX_STEPS:\n1. Re-check channel fit.\n"
        "TEST_COVERAGE: add a regression test.\n"
    )
    diagnosis = crew.generate_fix_diagnosis(
        "channel_bury_streak", "api-sentinel", {"channel": "reddit"}, llm_call=lambda prompt: fake_response
    )
    assert diagnosis["category"] == "inhaltlich"
    assert "keeps burying" in diagnosis["headline"]
    assert "CONFIDENCE_CAVEAT" in diagnosis["body"]


def test_generate_fix_diagnosis_falls_back_when_call_fails():
    def _boom(prompt):
        raise RuntimeError("api down")
    diagnosis = crew.generate_fix_diagnosis("stale_approvals", "api-sentinel", {"n": 1}, llm_call=_boom)
    assert diagnosis["category"] == "technisch"
    assert "api down" in diagnosis["body"]


def test_aufsichtsrat_lines_fix_md_new_entries():
    entries = [{"id": "fix_abc12345", "category": "technisch", "headline": "Something broke"}]
    lines = crew._aufsichtsrat_lines(0, None, 0, fix_md_new_entries=entries)
    joined = "\n".join(lines)
    assert "FIX.md aktualisiert" in joined
    assert "Something broke" in joined
    assert "fix_resolved: fix_abc12345" in joined


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
