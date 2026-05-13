"""
iMessage reply tool — universal outbound for Hermes-fleet agents.

Why this tool exists
────────────────────
Hermes-agent (Sancho, Quirm) already speaks BlueBubbles natively through
`gateway/platforms/bluebubbles.py`, but ONLY for messages that arrive via
its own webhook handler. When an iMessage from Leo arrives in the agent's
PVC inbox via `agent-bus` (because bb-proxy routed it there as part of
the new fleet-wide @-mention path), the runtime needs an in-context way
to send a reply that goes through the SAME path the other 5 agents use:

    agent → agent-bus /agent/<self>/reply → bb-proxy /imessage/<self>/send → Leo

This tool gives the LLM that path. Calling `imessage_reply(text="…")`
sends the message to Leo's iMessage with the `[<Name>] ` prefix added
server-side by agent-bus. Sancho/Quirm therefore no longer need to know
which path triggered them — they can always reply via this single tool
and Leo always sees a consistent sender tag.

The native BlueBubbles platform layer (gateway/platforms/bluebubbles.py)
is preserved as the path for *spontaneous* sends (cron jobs, hourly
check-ins) where there's no inbound message to reply to.

Auto-prefix rule
────────────────
agent-bus's /agent/<name>/reply endpoint adds the `[<Name>] ` prefix.
This tool must NOT pre-add it, otherwise Leo sees `[Sancho] [Sancho] …`.
The tool description below tells the LLM not to add its own prefix.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


# ─── Config ────────────────────────────────────────────────────────────

BUS_BASE_URL = os.environ.get(
    "A2A_BUS_BASE_URL",
    "http://agent-bus.agents-shared.svc.cluster.local:8080",
).rstrip("/")
BUS_TOKEN = os.environ.get("A2A_BUS_TOKEN", "")
BUS_TIMEOUT_S = float(os.environ.get("A2A_BUS_TIMEOUT_S", "15"))


def _agent_id() -> str:
    """Resolve THIS agent's lowercase id for the /agent/<id>/reply route."""
    aid = (
        os.environ.get("AGENT_ID")
        or os.environ.get("HERMES_AGENT_ID")
        or os.environ.get("HOSTNAME", "").split("-")[0]
        or "sancho"
    ).lower()
    return aid


# ─── Schema ────────────────────────────────────────────────────────────

IMESSAGE_REPLY_SCHEMA = {
    "name": "imessage_reply",
    "description": (
        "Send a reply to Leo over iMessage (BlueBubbles relay). Use this "
        "for *any* outbound iMessage that should reach Leo — whether you "
        "were triggered by an inbox file from the fleet bus, an A2A "
        "handoff, or a hand-rolled internal decision. The bridge auto-"
        "adds the `[<Name>] ` prefix so Leo always knows which agent "
        "replied; do NOT include your own name in `text` or it will be "
        "duplicated. House style: short messages, no markdown (asterisks "
        "and headers render raw on iMessage). For long replies, split "
        "into 2-3 short paragraphs.\n\n"
        "When NOT to use:\n"
        "  • Cron-driven proactive nudges that should respect the agent's "
        "    native rate-limit / quiet-hours rules — those still go via "
        "    the Hermes BlueBubbles platform layer.\n"
        "  • Messages to other agents — use `a2a_delegate` instead.\n\n"
        "Returns: { success, agent, to, delivery } on success, or "
        "{ success: false, error } on failure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Body of the iMessage. Do NOT include a `[<Name>] ` "
                    "prefix — that is added by agent-bus."
                ),
            },
            "to": {
                "type": "string",
                "description": (
                    "Optional iMessage handle override. Defaults to "
                    "LEO_IMESSAGE_HANDLE on agent-bus (Leo's phone)."
                ),
            },
            "p0": {
                "type": "boolean",
                "description": (
                    "Bypass quiet hours on bb-proxy (23:00-07:00 ET). "
                    "Reserved for genuinely urgent replies."
                ),
                "default": False,
            },
        },
        "required": ["text"],
    },
}


# ─── Handler ───────────────────────────────────────────────────────────


def imessage_reply(
    *, text: str, to: Optional[str] = None, p0: bool = False
) -> Dict[str, Any]:
    if not text or not str(text).strip():
        return {"success": False, "error": "text is required"}

    agent = _agent_id()
    url = f"{BUS_BASE_URL}/agent/{agent}/reply"
    headers = {"Content-Type": "application/json"}
    if BUS_TOKEN:
        headers["Authorization"] = f"Bearer {BUS_TOKEN}"

    body: Dict[str, Any] = {"text": text, "p0": bool(p0)}
    if to:
        body["to"] = to

    try:
        r = requests.post(url, json=body, headers=headers, timeout=BUS_TIMEOUT_S)
    except requests.RequestException as e:
        return {"success": False, "error": f"transport: {e}", "url": url}

    if r.status_code >= 300:
        return {
            "success": False,
            "error": f"http {r.status_code}: {r.text[:300]}",
            "url": url,
        }

    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    return {
        "success": True,
        "agent": agent,
        "to": data.get("to"),
        "delivery": data.get("delivery") or {},
        "note": "agent-bus auto-prefixed [<Name>] and forwarded via bb-proxy.",
    }


# ─── Registry ──────────────────────────────────────────────────────────

from tools.registry import registry  # noqa: E402


def _check_imessage_reply() -> Optional[str]:
    if not BUS_BASE_URL:
        return "A2A_BUS_BASE_URL (or BUS_BASE_URL fallback) is unset"
    return None


registry.register(
    name="imessage_reply",
    toolset="messaging",
    schema=IMESSAGE_REPLY_SCHEMA,
    handler=lambda args, **kw: imessage_reply(
        text=args.get("text", ""),
        to=args.get("to"),
        p0=bool(args.get("p0", False)),
    ),
    check_fn=_check_imessage_reply,
    emoji="📨",
)
