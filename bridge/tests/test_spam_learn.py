from types import SimpleNamespace

import spam
from parser import label_change


class Client:
    def __init__(self, email="deals@promo.example", others=(), status="open"):
        self.email, self.others, self.status = email, list(others), status
        self.notes, self.resolved = [], []

    def conversation_details(self, conversation_id):
        return {"id": conversation_id, "status": self.status,
                "meta": {"sender": {"id": 55, "email": self.email}}}

    def contact_conversations(self, contact_id):
        return [{"id": 9, "labels": ["spampromo"]}, *self.others]

    def resolve_conversation(self, conversation_id):
        self.resolved.append(conversation_id)
        return SimpleNamespace(ok=True)

    def post_private_note(self, conversation_id, content):
        self.notes.append(content)
        return SimpleNamespace(ok=True)


def test_labelling_resolves_and_teaches_the_exact_sender():
    client = Client()
    assert spam.learn_from_label(client, 9) == "learned"
    assert client.resolved == [9] and client.notes[0].startswith("Learned:")
    verdict = spam.evaluate_rules("Deals@Promo.example", "Your order is ready")
    assert verdict.spam and verdict.stage == "rule"  # exact sender beats a customer-ish subject
    assert not spam.evaluate_rules("other@promo.example", "x")  # never the whole domain


def test_removing_the_label_unlearns():
    client = Client()
    spam.learn_from_label(client, 9)
    assert spam.unlearn_from_label(client, 9) == "unlearned"
    assert spam.evaluate_rules("deals@promo.example", "x") is None


def test_a_sender_with_real_conversations_is_never_learned():
    client = Client(others=[{"id": 4, "labels": ["order"]}])
    assert spam.learn_from_label(client, 9) == "has_real_mail"
    assert client.resolved == [9] and client.notes[0].startswith("Not learned:")
    assert spam.evaluate_rules("deals@promo.example", "x") is None


def test_our_own_addresses_are_never_learned():
    assert spam.learn_sender("service@actuarialbookstore.com", 9) == "internal"


def test_learning_twice_writes_once():
    assert spam.learn_sender("a@b.example", 1) == "learned"
    assert spam.learn_sender("a@b.example", 2) == "already_known"


def test_label_change_reads_both_directions():
    def payload(prev, cur):
        return {"event": "conversation_updated", "id": 9,
                "changed_attributes": [{"label_list": {"previous_value": prev, "current_value": cur}}]}
    assert label_change(payload([], ["spampromo"]), "spampromo") == (9, "added")
    assert label_change(payload(["spampromo", "x"], ["x"]), "spampromo") == (9, "removed")
    assert label_change(payload(["x"], ["x", "dewie-draft"]), "spampromo") is None
