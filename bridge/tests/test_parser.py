from parser import draft_label_added, newest_customer_message, parse_message_created


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


def test_attachments_are_normalized_from_chatwoot_shape():
    value = payload()
    value["attachments"] = [{
        "file_type": "image",
        "file_name": "grade-slip.png",
        "data_url": "https://desk.example/rails/active_storage/blob/image",
    }]

    parsed = parse_message_created(value)

    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].file_type == "image"
    assert parsed.attachments[0].name == "grade-slip.png"
    assert parsed.attachments[0].data_url.endswith("/image")


def test_attachment_only_email_is_processable():
    value = payload(content="")
    value["attachments"] = [{
        "file_type": "application/pdf",
        "name": "order.pdf",
        "data_url": "https://desk.example/order.pdf",
    }]

    assert parse_message_created(value).should_process


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


def test_draft_label_requires_explicit_absent_to_present_transition():
    base = {"event": "conversation_updated", "id": 7}

    assert draft_label_added({
        **base,
        "changed_attributes": [{
            "label_list": {
                "previous_value": ["support"],
                "current_value": ["support", "dewie-draft"],
            }
        }],
    }, "dewie-draft") == 7
    assert draft_label_added({
        **base,
        "labels": ["dewie-draft"],
        "changed_attributes": [{"status": {"previous_value": "open", "current_value": "pending"}}],
    }, "dewie-draft") is None
    assert draft_label_added({
        **base,
        "changed_attributes": [{
            "label_list": {
                "previous_value": ["dewie-draft"],
                "current_value": ["dewie-draft"],
            }
        }],
    }, "dewie-draft") is None


def test_newest_customer_message_ignores_agent_activity_and_private_notes():
    messages = [
        {"id": 40, "message_type": 0, "content": "First", "sender": {"type": "contact"}},
        {"id": 41, "message_type": 1, "content": "Agent reply", "sender": {"type": "user"}},
        {"id": 42, "message_type": 0, "private": True, "content": "Private"},
        {"id": 43, "message_type": 2, "content": "Assigned"},
        {"id": 44, "message_type": 0, "content": "Second", "sender": {"type": "contact"}},
    ]

    parsed = newest_customer_message(
        messages,
        conversation_id=7,
        account_id=1,
        meta={
            "contact": {"email": "person@example.com", "name": "Person"},
            "additional_attributes": {"mail_subject": "Question"},
        },
    )

    assert parsed.message_id == 44
    assert parsed.body == "Second"
    assert parsed.from_email == "person@example.com"
    assert parsed.subject == "Question"

    bounded = newest_customer_message(
        messages,
        conversation_id=7,
        account_id=1,
        meta={"contact": {"email": "person@example.com"}},
        through_message_id=43,
    )
    assert bounded.message_id == 40
