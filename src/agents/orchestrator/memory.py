"""AgentCore Memory reads and writes.

Writes are immediate; extraction into long-term records runs in the background
and takes 30-60s, so nothing said this turn is recallable this turn.

Namespaces have to match the ones declared on the Memory resource in
infra/agents.yaml.

Strands has session managers that wrap this API. Calling it directly keeps the
mechanics visible, which is worth more here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

LOG = logging.getLogger(__name__)

# Data plane client. The control plane (bedrock-agentcore-control) creates
# memories; this one reads and writes their contents.
_client = boto3.client("bedrock-agentcore")

MEMORY_ID = os.environ.get("MEMORY_ID", "")
NAMESPACE_PREFIX = os.environ.get("MEMORY_NAMESPACE_PREFIX", "supplychain")

# How many records to pull per namespace. Small on purpose: retrieved memories
# are prepended to the system prompt, and every one of them is paid for as
# input tokens on every turn.
TOP_K = 5


def enabled() -> bool:
    """Memory is optional — the agent must still work without it."""
    return bool(MEMORY_ID)


def recall(actor_id: str, session_id: str, query: str) -> list[str]:
    """Return memories relevant to this query, newest and closest first.

    Semantic and preference namespaces are queried because they persist across
    sessions. The session summary namespace is queried too so that a resumed
    conversation has its own gist available.
    """
    if not enabled():
        return []

    namespaces = [
        f"{NAMESPACE_PREFIX}/user/{actor_id}/semantic",
        f"{NAMESPACE_PREFIX}/user/{actor_id}/preferences",
        f"{NAMESPACE_PREFIX}/user/{actor_id}/session/{session_id}/summary",
    ]

    memories: list[str] = []
    for namespace in namespaces:
        memories.extend(_retrieve(namespace, query))

    LOG.info("Recalled %s memories for actor %s", len(memories), actor_id)
    return memories


def _retrieve(namespace: str, query: str) -> list[str]:
    try:
        response = _client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=namespace,
            searchCriteria={"searchQuery": query, "topK": TOP_K},
        )
    except Exception as exc:  # noqa: BLE001
        # An empty namespace is normal, especially in the first minute of a
        # conversation. A memory miss must never break the turn.
        LOG.warning("Could not read namespace %s: %s", namespace, exc)
        return []

    texts: list[str] = []
    for record in response.get("memoryRecordSummaries", []):
        content = record.get("content", {})
        text = content.get("text") if isinstance(content, dict) else None
        if text:
            texts.append(text)

    return texts


def remember(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Write one conversational turn to short-term memory.

    Both sides of the exchange are written together because the extraction
    strategies read conversations, not isolated utterances — "I prefer detailed
    reports" is only meaningful alongside what was being discussed.
    """
    if not enabled():
        return

    try:
        _client.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            # Required by the API. Supplied by the caller rather than the
            # service so that a retry re-records the moment the exchange
            # happened, not the moment it was successfully written.
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {"conversational": {"role": "USER", "content": {"text": user_text}}},
                {
                    "conversational": {
                        "role": "ASSISTANT",
                        "content": {"text": assistant_text},
                    }
                },
            ],
        )
        LOG.info("Recorded turn for actor %s session %s", actor_id, session_id)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. This runs after the answer is already produced,
        # so anything raised here would throw away a good response over a
        # failed bookkeeping call. ClientError alone is not enough: a bad
        # request shape raises ParamValidationError, which is not a subclass.
        LOG.warning("Could not write memory event: %s", exc)


def as_prompt_section(memories: list[str]) -> str:
    """Render recalled memories for injection into the system prompt.

    They are explicitly labelled as recollection rather than fact. Without that
    framing a model will happily assert a stale memory as current truth.
    """
    if not memories:
        return ""

    bullets = "\n".join(f"- {memory}" for memory in memories)
    return (
        "\n\nWhat you remember about this user from previous conversations. "
        "Use these naturally — do not ask the user to confirm things you "
        "already remember, such as their name or how they like answers "
        "formatted. Do re-check anything that changes over time, like "
        "quantities, statuses or dates, with a tool before stating it:\n"
        f"{bullets}"
    )
