"""Secure one-to-one messaging (Phase 3: Secure Messaging).

Layers, from the wire up:

* :mod:`ghostlink.messaging.protocol` — authenticated key exchange and AEAD
  primitives (fresh session key per conversation, memory-only keys)
* :mod:`ghostlink.messaging.packets` — strictly validated secure-channel
  frames inside relay FORWARD bodies
* :mod:`ghostlink.messaging.models` — the message model and its delivery
  lifecycle
* :mod:`ghostlink.messaging.queue` — outgoing pump queue and incoming
  ordering/duplicate detection
* :mod:`ghostlink.messaging.session` — the chat session orchestrator
* :mod:`ghostlink.messaging.history` — optional, privacy-first retention
"""

from ghostlink.messaging.history import (
    BaseHistory,
    HistoryEntry,
    HistoryMode,
    open_history,
)
from ghostlink.messaging.models.message import (
    EncryptedPayload,
    Message,
    MessageDirection,
    MessageStatus,
)
from ghostlink.messaging.session.chat import (
    ChatEvent,
    ChatEventKind,
    ChatSession,
    ChatSessionConfig,
    ChatStats,
)

__all__ = [
    "BaseHistory",
    "ChatEvent",
    "ChatEventKind",
    "ChatSession",
    "ChatSessionConfig",
    "ChatStats",
    "EncryptedPayload",
    "HistoryEntry",
    "HistoryMode",
    "Message",
    "MessageDirection",
    "MessageStatus",
    "open_history",
]
