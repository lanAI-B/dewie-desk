from parser import parse_message_created


def payload(**updates):
    value = {
        "event": "message_created",
        "id": 42,
        "message_type": "incoming",
        "private": False,
        "content": "Please help with my access.",
        "content_attributes": {"email": {"subject": "Access question"}},
        "sender": {"email": "person@example.com"},
        "conversation": {
            "id": 7,
            "channel": "Channel::Email",
            "messages": [{"sender_type": "Contact"}],
        },
        "inbox": {"id": 3, "name": "Support"},
        "account": {"id": 1},
    }
    value.update(updates)
    return value


def test_real_email_shape_normalizes_message_and_subject():
    parsed = parse_message_created(payload())

    assert parsed.should_process
    assert parsed.message_id == 42
    assert parsed.conversation_id == 7
    assert parsed.sender_type == "contact"
    assert parsed.subject == "Access question"
    assert parsed.subject_source == "content_attributes.email.subject"


def test_per_message_subject_precedes_conversation_subject():
    value = payload()
    value["conversation"]["additional_attributes"] = {"mail_subject": "Old subject"}
    assert parse_message_created(value).subject == "Access question"


def test_conversation_subject_is_a_last_resort():
    value = payload(content_attributes={})
    value["conversation"]["additional_attributes"] = {"mail_subject": "Fallback"}
    assert parse_message_created(value).subject == "Fallback"


def test_non_customer_events_fail_closed_at_transport_gate():
    cases = [
        (payload(event="conversation_created"), "not_message_created"),
        (payload(message_type="outgoing"), "not_incoming"),
        (payload(private=True), "private_note"),
        (payload(sender={"email": "agent@example.com", "type": "user"}), "not_contact"),
        (payload(content=""), "empty_content"),
        (payload(sender={}), "missing_sender_email"),
        (payload(conversation={}), "missing_conversation_id"),
    ]

    for value, reason in cases:
        parsed = parse_message_created(value)
        assert not parsed.should_process
        assert parsed.skip_reason == reason


def test_garbage_payload_does_not_raise_or_process():
    parsed = parse_message_created({"conversation": "bad", "sender": []})
    assert not parsed.should_process
