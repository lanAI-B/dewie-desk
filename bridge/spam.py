"""Spam/noise auto-resolve for inbound Chatwoot email.

Order of evidence, cheapest and most predictable first:

1. ``spam_rules.csv`` — deterministic sender/domain/subject rules (data, not code).
2. The DewieOps desk classifier — only when ``SPAM_CLASSIFIER_ENABLED`` and no rule
   decided. It has no spam label, so only ``actor=system`` + ``intent=notify`` at or
   above ``SPAM_CLASSIFIER_MIN_CONFIDENCE`` counts as noise.

A spam verdict labels the conversation ``spam`` and resolves it. Nothing is ever
deleted, no draft is posted, and mail folders are never touched. Every verdict,
including dry-run ones, is written to the audit log so a false positive is findable:
search it for the conversation id or sender, or filter Chatwoot by the ``spam`` label.

Flags (all read per call, so a restart is not needed to change them in tests):
    SPAM_AUTORESOLVE_ENABLED   default false — master switch
    SPAM_AUTORESOLVE_DRY_RUN   default true  — log "would_resolve", write nothing
    SPAM_CLASSIFIER_ENABLED    default false — spend a classifier call per unmatched message
    SPAM_CLASSIFIER_MIN_CONFIDENCE  default 0.9
    SPAM_AUTORESOLVE_LOG       default <repo>/data/spam-autoresolve.jsonl
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SPAM_LABEL = "spam"
RULES_PATH = Path(__file__).resolve().parent / "spam_rules.csv"
_DEFAULT_LOG = Path(__file__).resolve().parent.parent / "data" / "spam-autoresolve.jsonl"
_MATCH_MODES = frozenset({"contains", "word", "word_case"})

log = logging.getLogger("dewie-desk-bridge.spam")
_audit_lock = threading.Lock()

# Subject words that mean a person is probably asking about their own business with
# us. They veto the broad stages (subject-only rules, classifier); a rule that names
# an exact sender is specific enough to stand.
_CUSTOMER_SUBJECT = re.compile(
    r"\b(order|orders|refund|return|exam|shipping|shipment|tracking|invoice|receipt|"
    r"access|login|password|textbook|manual|course|cancel|cancellation|backorder)\b"
    r"|#\s*\d{4,}",
    re.IGNORECASE,
)
_PUBLIC_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "aol.com", "msn.com", "proton.me",
    "protonmail.com", "comcast.net",
})


def _enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _enabled("SPAM_AUTORESOLVE_ENABLED", False)


def dry_run() -> bool:
    return _enabled("SPAM_AUTORESOLVE_DRY_RUN", True)


def classifier_enabled() -> bool:
    return _enabled("SPAM_CLASSIFIER_ENABLED", False)


def classifier_min_confidence() -> float:
    try:
        value = float(os.environ.get("SPAM_CLASSIFIER_MIN_CONFIDENCE", "0.9"))
    except ValueError:
        return 0.9
    # A floor below 0.5 would let a coin flip resolve mail; refuse to go that low.
    return min(max(value, 0.5), 1.0)


def audit_path() -> Path:
    configured = (os.environ.get("SPAM_AUTORESOLVE_LOG") or "").strip()
    return Path(configured) if configured else _DEFAULT_LOG


def status() -> dict:
    return {
        "enabled": enabled(),
        "dry_run": dry_run(),
        "classifier": classifier_enabled(),
        "rules": len(load_rules()),
    }


@dataclass(frozen=True)
class Rule:
    action: str
    sender: str
    subject: str
    subject_match: str
    source: str
    line: int

    @property
    def rule_id(self) -> str:
        return f"spam_rules.csv:{self.line}"

    @property
    def names_exact_sender(self) -> bool:
        return bool(self.sender) and not self.sender.startswith("@")

    def describe(self) -> str:
        parts = []
        if self.sender:
            parts.append(f"sender={self.sender}")
        if self.subject:
            parts.append(f"subject {self.subject_match} {self.subject!r}")
        return f"{self.rule_id} {self.action} " + " ".join(parts or ["(any)"])


def _rows(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        numbered = [
            (number, line) for number, line in enumerate(handle, start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]
    header, *body = numbered
    fields = next(csv.reader([header[1]]))
    for number, line in body:
        values = next(csv.reader([line]))
        yield number, dict(zip(fields, (value.strip() for value in values)))


_rules_cache: dict[Path, tuple[float, tuple[Rule, ...]]] = {}


def load_rules(path: Path | None = None) -> tuple[Rule, ...]:
    """Parse the rule file strictly; a malformed row fails loudly, never silently."""
    path = Path(path or RULES_PATH)
    mtime = path.stat().st_mtime
    cached = _rules_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    rules = []
    for number, row in _rows(path):
        action = row.get("action", "").lower()
        sender = row.get("sender", "").lower()
        subject = row.get("subject", "")
        mode = (row.get("subject_match") or "contains").lower()
        if action not in {"spam", "allow"}:
            raise ValueError(f"{path.name}:{number}: action must be spam or allow")
        if mode not in _MATCH_MODES:
            raise ValueError(f"{path.name}:{number}: unknown subject_match {mode!r}")
        if action == "spam" and not sender and not subject:
            raise ValueError(f"{path.name}:{number}: a spam rule needs a sender or subject")
        rules.append(Rule(action, sender, subject, mode, row.get("source", ""), number))
    result = tuple(rules)
    _rules_cache[path] = (mtime, result)
    return result


def _normalize_sender(value: str) -> str:
    value = (value or "").strip().lower()
    match = re.search(r"<([^>]+)>", value)
    return (match.group(1) if match else value).strip()


def _sender_ok(rule: Rule, sender: str) -> bool:
    if not rule.sender:
        return True
    if rule.sender.startswith("@"):
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        wanted = rule.sender[1:]
        return domain == wanted or domain.endswith("." + wanted)
    return sender == rule.sender


def _subject_ok(rule: Rule, subject: str) -> bool:
    if not rule.subject:
        return True
    if rule.subject_match == "contains":
        return rule.subject.lower() in subject.lower()
    flags = 0 if rule.subject_match == "word_case" else re.IGNORECASE
    return re.search(rf"(?<!\w){re.escape(rule.subject)}(?!\w)", subject, flags) is not None


def customer_signals(from_email: str, subject: str) -> list[str]:
    """Evidence that a real customer may be writing. Used for vetoes and review lists."""
    signals = []
    found = _CUSTOMER_SUBJECT.search(subject or "")
    if found:
        signals.append(f"subject mentions {found.group(0).strip()!r}")
    domain = _normalize_sender(from_email).rsplit("@", 1)[-1]
    if domain in _PUBLIC_PROVIDERS:
        signals.append(f"personal mailbox ({domain})")
    if domain.endswith(".edu") or domain.endswith(".ac.uk"):
        signals.append(f"academic domain ({domain})")
    return signals


@dataclass(frozen=True)
class Verdict:
    spam: bool
    stage: str  # rule | allow | classifier | veto | none
    reason: str
    rule: str = ""
    score: float | None = None


def evaluate_rules(from_email: str, subject: str, rules=None) -> Verdict | None:
    """Deterministic stage. ``None`` means no rule decided either way."""
    rules = load_rules() if rules is None else rules
    sender = _normalize_sender(from_email)
    subject = subject or ""
    matching = [r for r in rules if _sender_ok(r, sender) and _subject_ok(r, subject)]

    # 1. A spam rule naming this exact sender is the most specific evidence there is.
    for rule in matching:
        if rule.action == "spam" and rule.names_exact_sender:
            return Verdict(True, "rule", rule.describe(), rule.rule_id)
    # 2. Allow-listed senders are never resolved by anything broader.
    for rule in matching:
        if rule.action == "allow":
            return Verdict(False, "allow", rule.describe(), rule.rule_id)
    # 3. Domain and subject rules, unless the subject reads like a customer's.
    for rule in matching:
        if rule.action == "spam":
            signal = _CUSTOMER_SUBJECT.search(subject)
            if signal:
                return Verdict(
                    False, "veto",
                    f"{rule.describe()} vetoed: subject mentions {signal.group(0).strip()!r}",
                    rule.rule_id,
                )
            return Verdict(True, "rule", rule.describe(), rule.rule_id)
    return None


def evaluate_classification(classification, subject: str) -> Verdict:
    """Classifier stage: only confident system notifications count as noise."""
    if classification is None:
        return Verdict(False, "none", "classifier unavailable")
    actor = getattr(classification.actor, "value", classification.actor)
    intent = getattr(classification.intent, "value", classification.intent)
    score = min(classification.actor_confidence, classification.intent_confidence)
    detail = f"classifier {actor}/{intent} min_conf={score:.2f}"
    if actor != "system" or intent != "notify":
        return Verdict(False, "none", detail, score=score)
    if score < classifier_min_confidence():
        return Verdict(False, "none", f"{detail} below floor", score=score)
    signal = _CUSTOMER_SUBJECT.search(subject or "")
    if signal:
        return Verdict(
            False, "veto", f"{detail} vetoed: subject mentions {signal.group(0).strip()!r}",
            score=score,
        )
    return Verdict(True, "classifier", detail, score=score)


def evaluate(message, classify=None) -> Verdict:
    """Full decision for one parsed message; ``classify`` returns a Classification or raises."""
    verdict = evaluate_rules(message.from_email, message.subject)
    if verdict is not None:
        return verdict
    if classify is None or not classifier_enabled():
        return Verdict(False, "none", "no rule matched")
    try:
        classification = classify(message)
    except Exception as exc:  # a failed classifier must never resolve anything
        return Verdict(False, "none", f"classifier failed: {type(exc).__name__}")
    return evaluate_classification(classification, message.subject)


def audit(record: dict) -> None:
    """Append one JSON line per decision; also emitted to the bridge log."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    log.info("spam %s", json.dumps(record, sort_keys=True))
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        log.error("spam audit write failed path=%s error=%s", path, type(exc).__name__)


def screen(message, client, classify=None) -> str:
    """Decide one inbound message and act on it. Returns the outcome code.

    Outcomes: disabled, not_spam, vetoed, would_resolve, resolved, label_failed,
    resolve_failed. Label first, resolve second: a conversation that fails to
    resolve still carries the label a human can filter on.
    """
    if not enabled():
        return "disabled"
    verdict = evaluate(message, classify=classify)
    base = {
        "conversation_id": message.conversation_id,
        "message_id": message.message_id,
        "inbox_id": message.inbox_id,
        "sender": message.from_email,
        "subject": (message.subject or "")[:200],
        "stage": verdict.stage,
        "rule": verdict.rule,
        "reason": verdict.reason,
        "score": verdict.score,
    }
    if not verdict.spam:
        if verdict.stage == "veto":
            audit({**base, "outcome": "vetoed", "dry_run": dry_run()})
            return "vetoed"
        return "not_spam"
    if dry_run():
        audit({**base, "outcome": "would_resolve", "dry_run": True})
        return "would_resolve"

    labelled = client.add_label(int(message.conversation_id), SPAM_LABEL)
    if not labelled.ok:
        audit({**base, "outcome": "label_failed", "dry_run": False,
               "http_status": labelled.status_code})
        return "label_failed"
    resolved = client.resolve_conversation(int(message.conversation_id))
    if not resolved.ok:
        audit({**base, "outcome": "resolve_failed", "dry_run": False,
               "http_status": resolved.status_code})
        return "resolve_failed"
    audit({**base, "outcome": "resolved", "dry_run": False})
    return "resolved"


__all__ = [
    "SPAM_LABEL", "Rule", "Verdict", "audit", "audit_path", "classifier_enabled",
    "customer_signals", "dry_run", "enabled", "evaluate", "evaluate_classification",
    "evaluate_rules", "load_rules", "screen", "status",
]
