"""The Slice 3 offline replay, run as a test so the gates cannot loosen quietly."""

import copy
import json
import re

import pytest

import main
import shadow


@pytest.fixture(scope="module")
def summary():
    return shadow.summarize(shadow.replay())


@pytest.fixture
def corpus():
    return shadow.load_corpus()


# ── The plan's Slice 3 acceptance gates ───────────────────────────────────────

def test_no_system_or_notification_sample_is_draftable(summary):
    drafted = [
        case for case in summary["by_actor"] if case == "system"
    ]
    assert summary["acceptance_gates"]["no_system_or_notification_sample_is_draftable"]
    # Stated separately so a failure names the thing rather than a boolean.
    assert summary["by_intent"].get("notify", 0) >= 1, "corpus must contain a notify sample"
    assert drafted == ["system"], "corpus must contain a system-actor sample"


def test_unknown_and_low_confidence_reach_human_review(summary):
    assert summary["acceptance_gates"]["unknown_and_low_confidence_reach_human_review"]
    reasons = summary["by_reason_code"]
    for expected in ("unknown_actor", "unknown_intent", "low_confidence", "classifier_failed"):
        assert reasons.get(expected, 0) >= 1, f"corpus must exercise {expected}"


def test_legitimate_customer_requests_remain_draftable(summary):
    assert summary["acceptance_gates"]["legitimate_customer_requests_remain_draftable"]
    assert summary["totals"]["draftable"] >= 10


def test_corpus_and_policy_agree_about_where_a_classifier_is_reached(summary):
    assert summary["findings"]["corpus_defects"] == []


# ── The replay itself ─────────────────────────────────────────────────────────

def test_replay_spends_no_model_call_and_posts_nothing(monkeypatch):
    """The whole report must be producible with the drafter and client unusable."""
    def explode(*args, **kwargs):
        raise AssertionError("shadow replay reached a live model or the Chatwoot API")

    monkeypatch.setattr(main, "classifier_runtime", explode)
    monkeypatch.setattr(main, "chatwoot_client", explode)
    result = shadow.summarize(shadow.replay())
    assert result["model"]["drafter_calls"] == 0
    assert result["model"]["providers"] == {"offline-fixture": result["model"]["classifier_calls"]}


def test_every_case_is_accounted_for_exactly_once(summary):
    totals = summary["totals"]
    assert (
        totals["transport_filtered"]
        + totals["duplicate"]
        + totals["screened"]
    ) == totals["cases"]
    assert (
        totals["draftable"] + totals["triaged"] + totals["skipped"]
    ) == totals["screened"]


def test_every_case_matches_the_outcome_a_human_wrote_down(summary):
    findings = summary["findings"]
    assert findings["false_draft"] == []
    assert findings["false_skip"] == []
    assert findings["triaged_but_expected_draftable"] == []
    assert findings["other_disagreements"] == []


def test_redelivered_webhook_is_screened_once(summary):
    assert summary["totals"]["duplicate"] == 1
    assert summary["by_reason_code"]["duplicate_message"] == 1


def test_transport_filtered_traffic_never_reaches_the_classifier(summary):
    avoided = summary["model"]["classifier_calls_avoided"]
    # Transport-filtered and duplicate mail, plus the deterministic system
    # senders the policy rejects on the address alone.
    assert avoided == (
        summary["totals"]["transport_filtered"]
        + summary["totals"]["duplicate"]
        + 2
    )


def test_the_new_policy_only_ever_drafts_less_than_production(summary):
    comparison = summary["comparison_with_production_bridge"]
    assert comparison["new_drafts_legacy_does_not"] == [], (
        "a fail-closed replacement must never authorize a draft the current "
        "bridge refuses"
    )
    assert len(comparison["legacy_drafts_new_does_not"]) >= 5


def test_production_bridge_would_draft_on_a_classifier_failure(summary):
    """The disagreement that motivates the replacement, asserted rather than assumed."""
    failures = [
        item["case"]
        for item in summary["comparison_with_production_bridge"]["legacy_drafts_new_does_not"]
        if item["reason"] == "classifier_failed"
    ]
    assert failures == ["classifier-failed"]


# ── The corpus ────────────────────────────────────────────────────────────────

def test_corpus_carries_no_reachable_address(corpus):
    """Every fixture address must be unroutable, so a mistake cannot mail anyone."""
    allowed_suffixes = (".example", "example.com", "example.net", "actexlearning.com",
                        "actuarialbookstore.com")
    addresses = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                               json.dumps(corpus)))
    assert len(addresses) > 15, "the scan must actually be finding addresses"
    for address in addresses:
        domain = address.rsplit("@", 1)[-1]
        assert domain.endswith(allowed_suffixes), f"unsafe fixture address: {address}"


def test_case_ids_are_unique(corpus):
    ids = [case["id"] for case in corpus["cases"]]
    assert len(ids) == len(set(ids))


def test_absent_classification_means_the_classifier_must_not_be_reached(corpus):
    """Flip one system sender to an ordinary address; the corpus defect must surface."""
    mutated = copy.deepcopy(corpus)
    for case in mutated["cases"]:
        if case["id"] == "system-noreply-order-approval":
            case["merged_payload"]["sender"]["email"] = "person@example.net"
    report = shadow.replay(mutated)
    result = shadow.summarize(report)
    assert result["findings"]["corpus_defects"], (
        "a case that starts needing a classification must be reported, not guessed"
    )
    assert not shadow.gates_pass(result)


def test_report_renders_without_raising(summary):
    text = shadow.render(summary)
    assert "Acceptance gates" in text
    assert "no model call" in text


def test_demo_report_is_concise_and_states_its_safety_boundary(summary):
    text = shadow.render_demo(summary)

    assert len(text.splitlines()) <= 15
    assert "READY: safe to show" in text
    assert "no model, mailbox, Chatwoot API, private note, or customer send" in text
    assert "current-bridge drafts refused by replacement: 7" in text
    assert "classifier labels are recorded" in text


def test_demo_report_fails_closed_when_a_gate_fails(summary):
    changed = copy.deepcopy(summary)
    changed["acceptance_gates"]["no_corpus_defects"] = False

    text = shadow.render_demo(changed)

    assert "FAIL  policy gates: 3/4" in text
    assert "NOT READY: safe to show" in text


def test_demo_readiness_rejects_a_newly_authorized_draft(summary):
    changed = copy.deepcopy(summary)
    changed["comparison_with_production_bridge"]["new_drafts_legacy_does_not"] = [
        {"case": "regression", "reason": "drafted"}
    ]

    assert not shadow.demo_ready(changed)
    assert "NOT READY: safe to show" in shadow.render_demo(changed)
