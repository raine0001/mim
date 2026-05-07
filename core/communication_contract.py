from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = " ".join(str(item or "").strip().split())
        if text:
            normalized.append(text[:80])
    return normalized[:12]


@dataclass(slots=True)
class ExpertCommunicationReply:
    reply_text: str
    topic_hint: str = ""
    response_mode: str = "default"
    composer_mode: str = "deterministic_fallback"
    model: str = ""
    should_store_memory: bool = True
    memory_topics: list[str] = field(default_factory=list)
    memory_people: list[str] = field(default_factory=list)
    memory_events: list[str] = field(default_factory=list)
    memory_experiences: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "reply_text": self.reply_text,
            "topic_hint": self.topic_hint,
            "response_mode": self.response_mode,
            "composer_mode": self.composer_mode,
            "model": self.model,
            "should_store_memory": bool(self.should_store_memory),
            "memory_topics": list(self.memory_topics),
            "memory_people": list(self.memory_people),
            "memory_events": list(self.memory_events),
            "memory_experiences": list(self.memory_experiences),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "ExpertCommunicationReply | None":
        if not isinstance(payload, dict):
            return None
        reply_text = " ".join(str(payload.get("reply_text") or "").strip().split())
        if not reply_text:
            return None
        return cls(
            reply_text=reply_text,
            topic_hint=" ".join(str(payload.get("topic_hint") or "").strip().split())[:64],
            response_mode=" ".join(
                str(payload.get("response_mode") or "default").strip().split()
            )[:48]
            or "default",
            composer_mode=" ".join(
                str(payload.get("composer_mode") or "deterministic_fallback").strip().split()
            )[:48]
            or "deterministic_fallback",
            model=" ".join(str(payload.get("model") or "").strip().split())[:64],
            should_store_memory=bool(payload.get("should_store_memory", True)),
            memory_topics=_normalize_string_list(payload.get("memory_topics")),
            memory_people=_normalize_string_list(payload.get("memory_people")),
            memory_events=_normalize_string_list(payload.get("memory_events")),
            memory_experiences=_normalize_string_list(payload.get("memory_experiences")),
        )