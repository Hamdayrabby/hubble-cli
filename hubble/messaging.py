"""Inter-agent messaging protocol for reactive multi-agent swarm coordination.

Provides a thread-safe message bus enabling peer-to-peer, parent-to-subagent,
and broadcast communication without blocking or busy-polling.
"""

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_msg_counter = itertools.count(1)


@dataclass
class Message:
    id: str
    sender: str
    recipient: str
    content: str
    timestamp: float = field(default_factory=time.time)
    read: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp,
            "read": self.read,
        }


class MessageBus:
    """Thread-safe inter-agent message broker."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mailboxes: Dict[str, List[Message]] = {}
        self._agents: Dict[str, Dict[str, Any]] = {}

    def register_agent(self, agent_id: str, role: str = ""):
        with self._lock:
            key = agent_id.lower().strip()
            self._agents[key] = {
                "id": agent_id,
                "role": role,
                "registered_at": time.time(),
            }
            if key not in self._mailboxes:
                self._mailboxes[key] = []

    def unregister_agent(self, agent_id: str):
        with self._lock:
            key = agent_id.lower().strip()
            self._agents.pop(key, None)

    def is_registered(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id.lower().strip() in self._agents

    def list_active_agents(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._agents.values())

    def send(self, sender: str, recipient: str, content: str) -> Message:
        with self._lock:
            msg_id = f"msg-{next(_msg_counter)}"
            msg = Message(id=msg_id, sender=sender.strip(), recipient=recipient.strip(), content=content.strip())
            recip_key = recipient.lower().strip()

            if recip_key in ("broadcast", "all", "*"):
                # Send to all registered agents except sender
                sender_key = sender.lower().strip()
                for aid in list(self._agents):
                    if aid != sender_key:
                        if aid not in self._mailboxes:
                            self._mailboxes[aid] = []
                        bmsg = Message(id=f"msg-{next(_msg_counter)}", sender=sender.strip(), recipient=aid, content=content.strip())
                        self._mailboxes[aid].append(bmsg)
            else:
                if recip_key not in self._mailboxes:
                    self._mailboxes[recip_key] = []
                self._mailboxes[recip_key].append(msg)
            return msg

    def receive(self, recipient: str, mark_as_read: bool = True) -> List[Message]:
        with self._lock:
            recip_key = recipient.lower().strip()
            messages = self._mailboxes.get(recip_key, [])
            unread = [m for m in messages if not m.read]
            if mark_as_read:
                for m in unread:
                    m.read = True
            return unread

    def peek(self, recipient: str) -> List[Message]:
        with self._lock:
            recip_key = recipient.lower().strip()
            return [m for m in self._mailboxes.get(recip_key, []) if not m.read]

    def has_unread(self, recipient: str) -> bool:
        with self._lock:
            recip_key = recipient.lower().strip()
            return any(not m.read for m in self._mailboxes.get(recip_key, []))

    def clear(self, recipient: Optional[str] = None):
        with self._lock:
            if recipient:
                self._mailboxes.pop(recipient.lower().strip(), None)
            else:
                self._mailboxes.clear()
                self._agents.clear()


default_message_bus = MessageBus()
