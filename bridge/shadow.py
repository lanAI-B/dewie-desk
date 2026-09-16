"""Offline shadow replay of the desk decision policy (plan Slice 3, step 1).

Replays a sanitized fixture corpus through the *real* transport gate, the *real*
durable dedup store and the *real* DewieOps decision policy, then reports the
aggregate the plan asks for. It spends no model call, touches no mailbox, posts
no note and reaches no Chatwoot API, so it can be run on any box at any time.

What this proves and what it does not
-------------------------------------
Proved: given a classification, the policy decides the right thing; a
screened-out message never reaches the policy at all; a redelivered webhook is
screened once; and the fail-closed policy drafts strictly less than the bridge
running in production today.

NOT proved: that the classifier produces those classifications. The corpus
*records* the label rather than computing it, precisely so the report is
deterministic and free. Classifier accuracy is the QA/shadow half of Slice 3 and
needs a running Chatwoot - see ``docs/bridge-modernization-plan.md``.

Usage (from ``bridge/``)::

    python shadow.py            # text report
    python shadow.py --json     # machine-readable report
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


def _configure_dewieops_path() -> None:
    """Prefer the canonical sibling DewieOps checkout for the policy import."""
    configured = (os.environ.get("DEWIEOPS_PATH") or "").strip()
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[2] / "DewieOps"
    )
    if root.is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


_configure_dewieops_path()

import main
from state import DedupStore

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "shadow_corpus.json"

# Marks a case the corpus asserts never reaches a classifier, so a case that
# quietly starts needing a label is reported instead of silently guessed.
UNREACHED = object()


# ── The baseline: what the bridge running in production decides today ─────────

def legacy_decision(from_email: str, classification) -> str:
    """The current bridge's draft/no-draft rule, transcribed for comparison.

    Transcribed from the production DewieBrain checkout at ``eb66a53``:
    ``desk_bridge/app.py`` ``_classify`` (the deterministic system-sender check,
    then the model call, then the ``('unknown', 'unknown', 'GENERAL')``
    degradation) and ``_draft_and_post`` (the single ``actor == 'system'``
    guard). Its ``_is_system_sender`` at ``dewie_brain/desk/screener.py:124``
    carries the same local-part tuple as the DewieOps policy, so the same import
    is reused here rather than duplicating the list.

    It is a transcription, not an import: ``dewie-desk`` must have no import path
    into DewieBrain (plan, Slice 1 acceptance gate). Re-check it against those
    two functions before trusting a comparison taken from a later revision.
    """
    from dewie_brain.desk import Actor, is_system_sender

    if is_system_sender(from_email):
        return "skip"
    if classification is None:
        # The legacy comment reads "a hiccup never blocks a draft" - it degrades
        # to unknown/unknown/GENERAL and drafts anyway. This is the fail-OPEN
        # behaviour the new policy exists to replace.
        return "draft"
    if classification.actor is Actor.SYSTEM:
        return "skip"
    return "draft"


# ── Replay ────────────────────────────────────────────────────────────────────

@dataclass
class CaseOutcome:
    case_id: str
    note: str
    expected: str
    observed: str
    reason_code: str = ""
    category: str | None = None
    actor: str = ""
    intent: str = ""
    actor_confidence: float | None = None
    intent_confidence: float | None = None
    legacy: str = ""
    classifier_called: bool = False

    @property
    def agrees_with_expected(self) -> bool:
        return self.observed == self.expected


@dataclass
class ShadowReport:
    corpus: str
    corpus_path: str
    confidence_floor: float
    cases: list[CaseOutcome] = field(default_factory=list)
    bridge_counters: dict = field(default_factory=dict)
    defects: list[str] = field(default_factory=list)


def _merge(base, delta):
    """Overlay a case's delta on the corpus base, merging nested objects."""
    if not isinstance(base, dict) or not isinstance(delta, dict):
        return delta
    merged = dict(base)
    for key, value in delta.items():
        merged[key] = _merge(base.get(key), value) if key in base else value
    return merged


def load_corpus(path: Path = CORPUS_PATH) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    base = document.get("base") or {}
    for case in document["cases"]:
        case["merged_payload"] = _merge(base, case.get("payload") or {})
    return document


def _recorded_classification(case: dict):
    """Build the Classification the corpus says the classifier would return."""
    from dewie_brain.desk import Actor, Classification, Intent

    if "classification" not in case:
        return UNREACHED
    recorded = case["classification"]
    if recorded is None:
        return None
    return Classification(
        actor=Actor(recorded["actor"]),
        intent=Intent(recorded["intent"]),
        actor_confidence=float(recorded["actor_confidence"]),
        intent_confidence=float(recorded["intent_confidence"]),
        # Named so nobody can mistake this report for evidence about a provider.
        provider="offline-fixture",
        model="recorded-label",
    )


def _stub_classifier(case: dict, defects: list[str]):
    """The recorded classifier, plus an out-of-band channel for corpus defects.

    A defect cannot be signalled by raising: ``main._decision`` catches every
    ``Exception`` from the classifier and degrades to a fail-closed triage, so a
    raised defect would be indistinguishable from a genuine classifier failure -
    which is the one thing this sentinel exists to keep apart. It is appended to
    ``defects`` instead and read back after the decision returns.
    """
    from dewie_brain.desk import ClassificationError

    def classify(*, from_email, subject, body, hints):
        recorded = _recorded_classification(case)
        if recorded is UNREACHED:
            defects.append(
                f"{case['id']}: the classifier was reached, but the corpus "
                f"records no classification for it"
            )
            raise ClassificationError("corpus defect")
        if recorded is None:
            raise ClassificationError("recorded classifier failure")
        return recorded

    return classify


def replay(corpus: dict | None = None, *, state_db: Path | None = None) -> ShadowReport:
    """Run every corpus case through the real bridge path and report it."""
    corpus = corpus or load_corpus()
    floor = float(os.environ.get("DESK_CLASSIFIER_CONFIDENCE_FLOOR", "0.75"))
    report = ShadowReport(
        corpus=corpus.get("corpus", "unknown"),
        corpus_path=str(CORPUS_PATH),
        confidence_floor=floor,
    )

    previous_state, previous_metrics = main._state, main._metrics
    temporary = None
    if state_db is None:
        # ignore_cleanup_errors: DedupStore's ``with self._connect()`` commits
        # but does not close, so on Windows the file can still be held when the
        # replay ends. Leaving a temp file behind is not worth failing a report.
        temporary = tempfile.TemporaryDirectory(
            prefix="desk-shadow-", ignore_cleanup_errors=True
        )
        state_db = Path(temporary.name) / "shadow-state.sqlite3"
    try:
        main._state = DedupStore(state_db)
        main._metrics = Counter()
        for case in corpus["cases"]:
            report.cases.append(_replay_case(case, report))
        report.bridge_counters = dict(main._metrics)
    finally:
        main._state, main._metrics = previous_state, previous_metrics
        if temporary is not None:
            temporary.cleanup()
    return report


def _replay_case(case: dict, report: ShadowReport) -> CaseOutcome:
    parsed, verdict = main.ingest_message_created(case["merged_payload"])
    if not verdict["accepted"]:
        observed = (
            "duplicate" if verdict["reason"] == "duplicate_message" else "transport_filtered"
        )
        return CaseOutcome(
            case_id=case["id"],
            note=case.get("note", ""),
            expected=case["expected"],
            observed=observed,
            reason_code=verdict["reason"],
            legacy=observed,  # the transport gate is unchanged between the two
        )

    before = main._metrics.get("classifier_calls", 0)
    defects: list[str] = []
    decision = main._decision(parsed, classify=_stub_classifier(case, defects))
    if defects:
        report.defects.extend(defects)
        return CaseOutcome(
            case_id=case["id"],
            note=case.get("note", ""),
            expected=case["expected"],
            observed="corpus_defect",
        )
    classifier_called = main._metrics.get("classifier_calls", 0) > before

    classification = decision.classification
    recorded = _recorded_classification(case)
    return CaseOutcome(
        case_id=case["id"],
        note=case.get("note", ""),
        expected=case["expected"],
        observed=decision.action.value,
        reason_code=decision.reason_code,
        category=decision.category,
        actor=classification.actor.value if classification else "",
        intent=classification.intent.value if classification else "",
        actor_confidence=classification.actor_confidence if classification else None,
        intent_confidence=classification.intent_confidence if classification else None,
        legacy=legacy_decision(
            parsed.from_email, None if recorded is UNREACHED else recorded
        ),
        classifier_called=classifier_called,
    )


# ── Report ────────────────────────────────────────────────────────────────────

def _band(value: float | None) -> str:
    if value is None:
        return "none"
    return main._confidence_band(value)


def summarize(report: ShadowReport) -> dict:
    """The aggregate the plan's Slice 3 review report is required to show."""
    cases = report.cases
    screened = [case for case in cases if case.observed in {"draft", "triage", "skip"}]

    false_draft = [
        case for case in screened
        if case.observed == "draft" and case.expected in {"skip", "triage"}
    ]
    false_skip = [
        case for case in screened
        if case.observed == "skip" and case.expected in {"draft", "triage"}
    ]
    conservative = [
        case for case in screened
        if case.observed == "triage" and case.expected == "draft"
    ]
    already_reported = {
        case.case_id for case in false_draft + false_skip + conservative
    }
    other_disagreements = [
        case for case in cases
        if not case.agrees_with_expected and case.case_id not in already_reported
    ]

    legacy_only = [c for c in screened if c.legacy == "draft" and c.observed != "draft"]
    new_only = [c for c in screened if c.observed == "draft" and c.legacy != "draft"]

    system_or_notify_drafted = [
        case for case in screened
        if case.observed == "draft"
        and (case.actor == "system" or case.intent == "notify")
    ]
    weak = [
        case for case in screened
        if case.actor in {"unknown", ""}
        or case.intent == "unknown"
        or (case.actor_confidence is not None and case.actor_confidence < report.confidence_floor)
        or (case.intent_confidence is not None and case.intent_confidence < report.confidence_floor)
    ]
    weak_escaped = [case for case in weak if case.observed == "draft"]
    legitimate = [case for case in cases if case.expected == "draft"]
    legitimate_blocked = [case for case in legitimate if case.observed != "draft"]

    return {
        "corpus": report.corpus,
        "corpus_path": report.corpus_path,
        "confidence_floor": report.confidence_floor,
        "totals": {
            "cases": len(cases),
            "transport_filtered": sum(1 for c in cases if c.observed == "transport_filtered"),
            "duplicate": sum(1 for c in cases if c.observed == "duplicate"),
            "screened": len(screened),
            "draftable": sum(1 for c in screened if c.observed == "draft"),
            "triaged": sum(1 for c in screened if c.observed == "triage"),
            "skipped": sum(1 for c in screened if c.observed == "skip"),
        },
        "by_actor": dict(Counter(c.actor or "not_classified" for c in screened)),
        "by_intent": dict(Counter(c.intent or "not_classified" for c in screened)),
        "by_actor_confidence_band": dict(Counter(_band(c.actor_confidence) for c in screened)),
        "by_intent_confidence_band": dict(Counter(_band(c.intent_confidence) for c in screened)),
        "by_reason_code": dict(Counter(c.reason_code for c in cases if c.reason_code)),
        "by_draft_category": dict(Counter(c.category for c in screened if c.category)),
        "model": {
            "classifier_calls": sum(1 for c in screened if c.classifier_called),
            "classifier_calls_avoided": sum(
                1 for c in cases if not c.classifier_called and c.observed != "corpus_defect"
            ),
            # Zero because the replay never drafts, which is the point of it.
            # The two "avoided" lines are kept apart because they are different
            # savings: one is the policy refusing, one is transport never
            # offering, and adding them together would flatter the policy.
            "drafter_calls": 0,
            "drafter_calls_policy_would_authorize": sum(
                1 for c in screened if c.observed == "draft"
            ),
            "drafter_calls_avoided_by_policy": sum(
                1 for c in screened if c.observed != "draft"
            ),
            "drafter_calls_avoided_before_policy": sum(
                1 for c in cases if c.observed in {"transport_filtered", "duplicate"}
            ),
            "providers": dict(
                Counter(
                    "offline-fixture" for c in screened if c.classifier_called
                )
            ),
        },
        "comparison_with_production_bridge": {
            "agree_on_drafting": sum(
                1 for c in screened if (c.legacy == "draft") == (c.observed == "draft")
            ),
            "legacy_drafts_new_does_not": [
                {"case": c.case_id, "reason": c.reason_code, "note": c.note} for c in legacy_only
            ],
            "new_drafts_legacy_does_not": [
                {"case": c.case_id, "reason": c.reason_code, "note": c.note} for c in new_only
            ],
        },
        "findings": {
            "false_draft": [{"case": c.case_id, "expected": c.expected, "note": c.note} for c in false_draft],
            "false_skip": [{"case": c.case_id, "expected": c.expected, "note": c.note} for c in false_skip],
            "triaged_but_expected_draftable": [
                {"case": c.case_id, "reason": c.reason_code, "note": c.note} for c in conservative
            ],
            "other_disagreements": [
                {"case": c.case_id, "expected": c.expected, "observed": c.observed} for c in other_disagreements
            ],
            "corpus_defects": report.defects,
        },
        "acceptance_gates": {
            "no_system_or_notification_sample_is_draftable": not system_or_notify_drafted,
            "unknown_and_low_confidence_reach_human_review": not weak_escaped,
            "legitimate_customer_requests_remain_draftable": not legitimate_blocked,
            "no_corpus_defects": not report.defects,
        },
        "bridge_counters": report.bridge_counters,
    }


def _counts(title: str, mapping: dict) -> list[str]:
    lines = [f"  {title}"]
    if not mapping:
        lines.append("    (none)")
    for key, value in sorted(mapping.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"    {key:<34} {value}")
    return lines


def render(summary: dict) -> str:
    totals = summary["totals"]
    lines = [
        "Desk shadow replay - offline half of Slice 3",
        f"corpus {summary['corpus']} ({totals['cases']} cases), "
        f"confidence floor {summary['confidence_floor']}",
        "no model call, no mailbox, no Chatwoot API, no note posted",
        "",
        "Transport and decision",
        f"    screened                           {totals['screened']}",
        f"    transport-filtered                 {totals['transport_filtered']}",
        f"    duplicate                          {totals['duplicate']}",
        f"    draftable                          {totals['draftable']}",
        f"    triaged                            {totals['triaged']}",
        f"    skipped                            {totals['skipped']}",
        "",
    ]
    lines += _counts("By actor", summary["by_actor"]) + [""]
    lines += _counts("By intent", summary["by_intent"]) + [""]
    lines += _counts("By actor confidence band", summary["by_actor_confidence_band"]) + [""]
    lines += _counts("By intent confidence band", summary["by_intent_confidence_band"]) + [""]
    lines += _counts("By reason code", summary["by_reason_code"]) + [""]
    lines += _counts("By drafter category", summary["by_draft_category"]) + [""]

    model = summary["model"]
    lines += [
        "  Model usage",
        f"    classifier calls                   {model['classifier_calls']}",
        f"    classifier calls avoided           {model['classifier_calls_avoided']}",
        f"    drafter calls made                 {model['drafter_calls']}",
        f"    drafter calls policy would allow   {model['drafter_calls_policy_would_authorize']}",
        f"    drafter calls refused by policy    {model['drafter_calls_avoided_by_policy']}",
        f"    never offered by transport         {model['drafter_calls_avoided_before_policy']}",
        f"    providers                          {model['providers'] or '{}'}",
        "",
    ]

    comparison = summary["comparison_with_production_bridge"]
    lines += ["  Against the bridge running in production today"]
    lines.append(
        f"    agree on drafting                  {comparison['agree_on_drafting']}"
    )
    lines.append(
        f"    it would draft, this does not      {len(comparison['legacy_drafts_new_does_not'])}"
    )
    for item in comparison["legacy_drafts_new_does_not"]:
        lines.append(f"      - {item['case']} ({item['reason']})")
    lines.append(
        f"    this drafts, it would not          {len(comparison['new_drafts_legacy_does_not'])}"
    )
    for item in comparison["new_drafts_legacy_does_not"]:
        lines.append(f"      - {item['case']} ({item['reason']})")
    lines.append("")

    findings = summary["findings"]
    lines += ["  Findings"]
    for name in (
        "false_draft",
        "false_skip",
        "triaged_but_expected_draftable",
        "other_disagreements",
        "corpus_defects",
    ):
        entries = findings[name]
        lines.append(f"    {name:<34} {len(entries)}")
        for entry in entries:
            lines.append(f"      - {entry if isinstance(entry, str) else entry['case']}")
    lines.append("")

    lines += ["  Acceptance gates"]
    for gate, passed in summary["acceptance_gates"].items():
        lines.append(f"    {'PASS' if passed else 'FAIL'}  {gate}")
    return "\n".join(lines)


def gates_pass(summary: dict) -> bool:
    return all(summary["acceptance_gates"].values())


def demo_ready(summary: dict) -> bool:
    """Require both policy gates and a one-way-safe legacy comparison."""
    return (
        gates_pass(summary)
        and not summary["findings"]["false_draft"]
        and not summary["comparison_with_production_bridge"]["new_drafts_legacy_does_not"]
    )


def render_demo(summary: dict) -> str:
    """Render the small, candid report used for a live desk walkthrough."""
    gates = summary["acceptance_gates"]
    findings = summary["findings"]
    comparison = summary["comparison_with_production_bridge"]
    false_drafts = len(findings["false_draft"])
    legacy_refused = len(comparison["legacy_drafts_new_does_not"])
    reverse_regressions = len(comparison["new_drafts_legacy_does_not"])
    passed = sum(gates.values())
    ready = demo_ready(summary)

    return "\n".join(
        [
            "Desk demo readiness - offline policy replay",
            f"corpus: {summary['corpus']} ({summary['totals']['cases']} synthetic cases)",
            "safety: no model, mailbox, Chatwoot API, private note, or customer send",
            "",
            f"{'PASS' if passed == len(gates) else 'FAIL'}  policy gates: {passed}/{len(gates)}",
            f"{'PASS' if false_drafts == 0 else 'FAIL'}  false drafts: {false_drafts}",
            f"{'PASS' if reverse_regressions == 0 else 'FAIL'}  newly-authorized drafts vs current bridge: {reverse_regressions}",
            f"INFO  current-bridge drafts refused by replacement: {legacy_refused}",
            f"INFO  offline drafter calls made: {summary['model']['drafter_calls']}",
            "",
            f"{'READY' if ready else 'NOT READY'}: safe to show the deterministic policy replay",
            "Limit: classifier labels are recorded; live classifier and transport need attended QA.",
        ]
    )


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit the report as JSON")
    output.add_argument(
        "--demo",
        action="store_true",
        help="emit a concise, presentation-ready readiness report",
    )
    parser.add_argument("--out", help="also write the report to this path")
    args = parser.parse_args(argv)

    if args.demo:
        # The corpus deliberately includes a classifier failure. Its warning is
        # useful in service logs but is expected noise in this offline report.
        logging.getLogger("dewie-desk-bridge").setLevel(logging.CRITICAL)
    summary = summarize(replay())
    if args.json:
        text = json.dumps(summary, indent=2)
    elif args.demo:
        text = render_demo(summary)
    else:
        text = render(summary)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    ready = demo_ready(summary) if args.demo else gates_pass(summary)
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main_cli())
