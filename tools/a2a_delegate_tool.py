"""
A2A delegation tool — publishes a cross-agent task into the fleet bus.

The original `delegate_task` tool spawns *in-process subagents* and is
unrelated to cross-fleet handoffs. Inter-agent comms ride the A2A
protocol (see `~/.hermes/skills/a2a-task-routing/SKILL.md` for the
contract), which is implemented by the `agent-bus` substrate service.

This tool POSTs to one of two bus endpoints:

  - `POST /agent/<target>/a2a` — direct fire-and-forget. The bus drops
    an inbox page on the target's RWX graph PVC, INSERTs a
    `handoff.requested` row into `fleet_events`, and (if the target's
    harness has a native HTTP injection URL wired up) POSTs there too.

  - `POST /fleet/publish` — broadcast or non-direct events
    (e.g. `kind="broadcast.fyi"`, target=None). The bus's own
    LISTEN/NOTIFY consumer then routes to each interested agent.

The tool does NOT block waiting for the target agent to act on the
task — A2A is asynchronous by design. The return value is just an
event id + delivery receipt. Status of the delegated work surfaces
later as another `handoff.accepted` / `handoff.completed` event on
the bus.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Bus base URL — defaults to cluster-internal ClusterIP. Settable per-pod
# via env, so local-dev / testing can point at a stub.
A2A_BUS_BASE_URL = os.environ.get(
    "A2A_BUS_BASE_URL",
    "http://agent-bus.agents-shared.svc.cluster.local:8080",
).rstrip("/")

# Optional shared auth — empty unless cluster posture later requires it.
A2A_BUS_TOKEN = os.environ.get("A2A_BUS_TOKEN", "")

A2A_BUS_TIMEOUT_S = float(os.environ.get("A2A_BUS_TIMEOUT_S", "10"))

# Canonical fleet roster — keep in sync with `agent-bus`'s AGENTS list
# and `homelab/argocd/apps/agents.yaml`. Validating here gives the model
# a clean error when it makes up an agent name.
A2A_AGENTS = frozenset(
    ["vetinari", "frick", "frack", "sancho", "quirm", "vimes", "puck"]
)

# Allowed kinds — taxonomy from `scripts/agents/sql/fleet-schema.sql`.
A2A_KINDS = frozenset(
    [
        "handoff.requested",
        "handoff.accepted",
        "handoff.rejected",
        "handoff.completed",
        "incident.opened",
        "incident.resolved",
        "audit.violation",
        "audit.ok",
        "knowledge.published",
        "knowledge.updated",
        "schedule.proposed",
        "schedule.confirmed",
        "broadcast.fyi",
    ]
)

# severity enum from the fleet_events CHECK constraint.
A2A_SEVERITIES = frozenset(["info", "warn", "critical", "p0"])


# ─── Schema (OpenAI-function-style) ────────────────────────────────────

A2A_DELEGATE_SCHEMA = {
    "name": "a2a_delegate",
    "description": (
        "Hand off a task to another agent in the fleet via the A2A protocol.\n\n"
        "Use this — NOT `delegate_task` — whenever you want another agent (Frick, "
        "Vetinari, Frack, Quirm, Vimes, Puck) to actually do work. `delegate_task` "
        "only spawns in-process subagents inside YOUR own runtime and cannot reach "
        "other agents.\n\n"
        "Semantics: A2A is asynchronous. This tool publishes a `handoff.requested` "
        "event onto the fleet bus and returns immediately with an event id. The "
        "target agent picks it up on its next inbox sweep (sub-hour latency in the "
        "worst case; near-instant when the target's native HTTP injection URL is "
        "wired). Don't poll for completion in the same turn — the target will "
        "publish a `handoff.completed` or `handoff.rejected` event back, which you "
        "see via your normal inbox path.\n\n"
        "For broadcasts (FYI to the whole fleet), set kind='broadcast.fyi' and "
        "omit target — the bus fans out to every interested consumer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Target agent: vetinari, frick, frack, quirm, vimes, or puck. "
                    "Omit for broadcasts (set kind='broadcast.fyi' instead)."
                ),
                "enum": sorted(A2A_AGENTS),
            },
            "goal": {
                "type": "string",
                "description": (
                    "One-sentence task description. The target sees this verbatim "
                    "as the headline — keep it self-contained, no chat references."
                ),
            },
            "task_type": {
                "type": "string",
                "description": (
                    "What kind of work: investigate, implement, review, audit, "
                    "research, design, monitor. Free-form but keep it short."
                ),
                "default": "investigate",
            },
            "priority": {
                "type": "string",
                "description": "P0 wakes target out of quiet hours; P1/P2/P3 queue.",
                "enum": ["P0", "P1", "P2", "P3"],
                "default": "P2",
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional structured context the target needs: background, "
                    "deliverables, constraints, references. Keep payload < 1 MB."
                ),
            },
            "task_id": {
                "type": "string",
                "description": (
                    "Stable id for this handoff (e.g. 2026-05-12-repo-triage). "
                    "Lets you correlate handoff.completed events later. "
                    "Generated if omitted."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Fleet event kind. Defaults to 'handoff.requested' for "
                    "directed tasks. Use 'broadcast.fyi' for fleet-wide FYIs."
                ),
                "enum": sorted(A2A_KINDS),
                "default": "handoff.requested",
            },
            "deliverables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of expected deliverables.",
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of constraints / non-goals.",
            },
        },
        "required": ["goal"],
    },
}


# ─── Handler ───────────────────────────────────────────────────────────


def _priority_to_severity(priority: str) -> str:
    return {"P0": "p0", "P1": "warn", "P2": "info", "P3": "info"}.get(
        (priority or "").upper(), "info"
    )


def _bus_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{A2A_BUS_BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if A2A_BUS_TOKEN:
        headers["Authorization"] = f"Bearer {A2A_BUS_TOKEN}"
    try:
        r = requests.post(url, json=body, headers=headers, timeout=A2A_BUS_TIMEOUT_S)
    except requests.RequestException as e:
        return {"ok": False, "error": f"transport: {e}", "url": url}
    if r.status_code >= 300:
        return {"ok": False, "error": f"http {r.status_code}: {r.text[:200]}", "url": url}
    try:
        return {"ok": True, "response": r.json(), "url": url}
    except ValueError:
        return {"ok": True, "response": {"raw": r.text[:200]}, "url": url}


def a2a_delegate(
    *,
    goal: str,
    target: Optional[str] = None,
    task_type: str = "investigate",
    priority: str = "P2",
    context: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    kind: str = "handoff.requested",
    deliverables: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    source_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish an A2A task onto the fleet bus.

    Returns a dict with `success`, the bus `event_id`, and the delivery
    receipt (file sink + native HTTP best-effort). The caller's hermes
    runtime renders this back as a tool result; the actual response from
    the target agent arrives later as a separate inbox / bus event.
    """
    if not goal or not str(goal).strip():
        return {"success": False, "error": "goal is required"}
    if target and target not in A2A_AGENTS:
        return {
            "success": False,
            "error": f"unknown target {target!r}; valid: {sorted(A2A_AGENTS)}",
        }
    if kind not in A2A_KINDS:
        return {
            "success": False,
            "error": f"unknown kind {kind!r}; valid: {sorted(A2A_KINDS)}",
        }
    if kind != "broadcast.fyi" and not target:
        return {
            "success": False,
            "error": "target is required unless kind='broadcast.fyi'",
        }

    if not task_id:
        task_id = (
            f"{time.strftime('%Y-%m-%d')}-{kind.replace('.', '-')}-{uuid.uuid4().hex[:8]}"
        )

    # Best-effort discover the source agent: env (set by the deployment),
    # then HOSTNAME-derived guess, then hardcoded fallback ('sancho') so
    # local-dev runs don't crash.
    if not source_agent:
        source_agent = (
            os.environ.get("AGENT_ID")
            or os.environ.get("HERMES_AGENT_ID")
            or os.environ.get("HOSTNAME", "").split("-")[0]
            or "sancho"
        )

    payload = {
        "source_agent": source_agent,
        "target_agent": target,
        "task_id": task_id,
        "task_type": task_type,
        "priority": priority,
        "goal": goal.strip(),
        "context": context or {},
        "deliverables": deliverables or [],
        "constraints": constraints or [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Direct delegation uses /agent/<target>/a2a; broadcasts use /fleet/publish.
    if target and kind == "handoff.requested":
        bus = _bus_post(f"/agent/{target}/a2a", payload)
    else:
        bus = _bus_post(
            "/fleet/publish",
            {
                "agent": source_agent,
                "kind": kind,
                "target": target,
                "severity": _priority_to_severity(priority),
                "payload": payload,
            },
        )

    if not bus["ok"]:
        return {
            "success": False,
            "error": bus["error"],
            "bus_url": bus["url"],
            "task_id": task_id,
        }

    resp = bus["response"]
    event_id = resp.get("event_id")
    return {
        "success": True,
        "task_id": task_id,
        "target": target,
        "kind": kind,
        "event_id": event_id,
        "source_agent": source_agent,
        "delivery": {
            "file": (resp.get("file") or {}).get("status"),
            "http": (resp.get("http") or {}).get("status"),
        },
        "note": (
            "Async handoff published. Target picks up on next inbox sweep. "
            "Look for a `handoff.completed` or `handoff.rejected` event for status."
        ),
    }


# ─── Registry ──────────────────────────────────────────────────────────

from tools.registry import registry  # noqa: E402  (avoid circular import on load)


def _check_a2a_requirements() -> Optional[str]:
    """Cheap availability check: bus URL configured? (We don't ping at register time.)"""
    if not A2A_BUS_BASE_URL:
        return "A2A_BUS_BASE_URL is unset"
    return None


registry.register(
    name="a2a_delegate",
    toolset="a2a",
    schema=A2A_DELEGATE_SCHEMA,
    handler=lambda args, **kw: a2a_delegate(
        goal=args.get("goal", ""),
        target=args.get("target"),
        task_type=args.get("task_type", "investigate"),
        priority=args.get("priority", "P2"),
        context=args.get("context"),
        task_id=args.get("task_id"),
        kind=args.get("kind", "handoff.requested"),
        deliverables=args.get("deliverables"),
        constraints=args.get("constraints"),
    ),
    check_fn=_check_a2a_requirements,
    emoji="🤝",
)
