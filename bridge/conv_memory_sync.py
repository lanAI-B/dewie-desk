"""Write a Chatwoot-sent reply into DewieOps ``conv_memory`` (#6334).

The row mirrors the one DewieBrain's ``email_dewie/sent_sync.py`` writes for a
human reply found in the Sent folder - role ``assistant``, the customer's
address as ``customer_id``, the subject as ``topic``, content capped at 2000,
``turn_index`` 0 - with two deliberate differences:

* ``agent_id`` is ``chatwoot``: conv_memory has no source column, and agent_id
  is where the writer is recorded. The analyst maps any agent_id it does not
  recognise as a bot to resolution "human", which is what these are.
* ``session_id`` is ``chatwoot:<account>:<message id>`` rather than the RFC822
  Message-ID. conv_memory.session_id is varchar(64) and Chatwoot's ids
  (``conversation/<uuid>/messages/<id>@<domain>``) are longer than that; the
  Chatwoot message id is also the dedup key the task names.

``created_at`` is the time Chatwoot created the message, not the insert time,
so history ordering matches what the customer saw.

Only DewieOps is imported (schema + engine), never DewieBrain.
"""

from __future__ import annotations

AGENT_ID = "chatwoot"
CONTENT_LIMIT = 2000
TOPIC_LIMIT = 128


def session_id_for(sent) -> str:
    return f"chatwoot:{sent.account_id}:{sent.message_id}"


def row_for(sent, subject: str) -> dict:
    content = sent.content
    if sent.attachment_names:
        content = f"{content}\n[attachments: {', '.join(sent.attachment_names)}]".strip()
    return {
        "session_id": session_id_for(sent),
        "turn_index": 0,
        "customer_id": sent.customer_email or None,
        "agent_id": AGENT_ID,
        "role": "assistant",
        "content": (content or "(no body)")[:CONTENT_LIMIT],
        "topic": (subject or "(no subject)")[:TOPIC_LIMIT],
        "created_at": sent.created_at,
    }


class ConvMemoryWriter:
    """Insert-if-absent on session_id, inside one transaction."""

    def __init__(self, engine=None):
        self._engine = engine

    def _get_engine(self):
        if self._engine is None:
            from dewie_brain.db.connection import get_engine

            self._engine = get_engine()
        return self._engine

    def write(self, sent, subject: str = "") -> bool:
        """True when a row was inserted, False when this message was already there."""
        import sqlalchemy
        from dewie_brain.db.schema import conv_memory

        row = row_for(sent, subject)
        with self._get_engine().begin() as conn:
            existing = conn.execute(
                sqlalchemy.select(conv_memory.c.id)
                .where(conv_memory.c.session_id == row["session_id"])
                .limit(1)
            ).first()
            if existing is not None:
                return False
            conn.execute(conv_memory.insert().values(**row))
        return True
