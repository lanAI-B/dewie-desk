from thread import build_draft_thread, fetch_all_messages
from chatwoot import ConversationResult


def msg(i, kind, content, name="", private=False):
    return {"id": i, "message_type": {"in": 0, "out": 1, "act": 2}[kind], "private": private,
            "content": content, "sender": {"name": name}}


THREAD = [
    msg(1, "in", "I ordered FAM in May, order 1280400.", "Jo"),
    msg(2, "act", "Conversation was assigned"),
    msg(3, "out", "**Dewie draft** (private; m)\n\nHi Jo", private=True),
    msg(4, "out", "It ships Friday.", "Lana"),
    msg(5, "out", "[sent by Marta via Outlook]\n\nTracking to follow.", private=True),
    msg(6, "out", "check SKW first", "Marta", private=True),
    msg(7, "in", "Any update?", "Jo"),
    msg(8, "out", "@dewie tell her it shipped today.", "Lana", private=True),
    msg(9, "out", "**Dewie's doubts** (drafted anyway)", private=True),
    msg(10, "out", "Paula, can you check SKW?", "Lana", private=True),
]


def test_thread_is_everything_said_and_tagged_notes_steer():
    t = build_draft_thread(THREAD, customer_message_id=7, through_message_id=10)
    assert "order 1280400" in t.thread and "It ships Friday." in t.thread
    assert "sent to the customer from Outlook" in t.thread
    assert "Internal note from Marta (team only" in t.thread
    assert "Dewie" not in t.thread and "assigned" not in t.thread
    assert t.thread.index("[#1]") < t.thread.index("[#7]")
    assert t.team_notes == "Lana: tell her it shipped today."
    # Untagged chatter after the message is context, not an order.
    assert "Paula, can you check SKW?" in t.thread
    assert "SKW" not in t.team_notes
    assert t.newest_note_id == 10


def test_a_chatwoot_mention_counts_as_the_tag():
    note = msg(8, "out", "[@Dewie](mention://user/9/Dewie) refund as an exception", "Lana", private=True)
    t = build_draft_thread(THREAD[:7] + [note], customer_message_id=7)
    assert t.team_notes == "Lana: refund as an exception"


def test_a_lone_message_has_no_thread_and_later_notes_past_the_command_are_ignored():
    t = build_draft_thread(THREAD[:1] + [THREAD[7]], customer_message_id=1, through_message_id=1)
    assert t == type(t)()


def test_long_threads_keep_the_first_and_newest_messages():
    many = [msg(1, "in", "FIRST " * 50)] + [msg(i, "in", f"m{i} " + "x" * 300) for i in range(2, 60)]
    t = build_draft_thread(many, customer_message_id=59, budget=3000)
    assert t.thread.startswith("[#1]") and "[#59]" in t.thread
    assert "earlier message(s) omitted" in t.thread and len(t.thread) <= 3000


def test_fetch_pages_back_until_chatwoot_runs_out():
    pages = {None: [msg(i, "in", str(i)) for i in range(21, 41)],
             21: [msg(i, "in", str(i)) for i in range(1, 21)], 1: []}

    class Client:
        def get_conversation(self, conversation_id, before=None):
            return ConversationResult(True, tuple(pages[before]), {}, 200, "fetched")

    got = fetch_all_messages(Client(), 7, pages[None])
    assert [m["id"] for m in got] == list(range(1, 41))
