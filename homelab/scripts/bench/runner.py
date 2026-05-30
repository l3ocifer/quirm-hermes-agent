#!/usr/bin/env python3
"""Quirm benchmark runner — pops one queued experiment and runs it.

Karpathy-style auto-research loop, runner half:

  while queue not empty:
      exp  = SELECT next experiment FOR UPDATE SKIP LOCKED
      mark exp as running
      try:
          result, metrics = call_litellm(model, params, prompt)
          correctness = grade(result, ...)          # deterministic, where one exists
          quality     = judge(prompt, result)       # pinned cross-family judge
          composite   = blend(correctness, quality, latency, cost)
          mark exp done with result + metrics + per-axis scores + rationale
      except Exception as e:
          mark exp error with traceback
      maybe emit a finding (low quality + rationale | accuracy cliff | regression)

Scoring is multi-dimensional, not binary — see EVAL-ARCHITECTURE.md. Every run
gets a graded, comparable composite (a truncated/refused/partial answer is never
silently 0), and the judge's rationale is stored so rankings are auditable.

The proposer (proposer.py) is what makes this a *research loop* —
it reads bench.experiments and bench.findings to choose the next set
of configs to test. This script just executes the top-priority queued
experiment one at a time so we never hammer LiteLLM.

Designed to run as a Kubernetes CronJob every minute. If the queue
is empty it exits 0 immediately. If a run is in flight on a sibling
pod, the FOR UPDATE SKIP LOCKED clause guarantees we don't double-run.

Timeouts are per-model and deliberately generous — see MODEL_TIMEOUTS
below for how to tune them. This is an unattended loop with no deadline
pressure, so we always prefer a slow complete answer over a fast
truncated one.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row


LITELLM_BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
LITELLM_KEY = os.environ["LITELLM_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
RUN_BUDGET_S = int(os.environ.get("BENCH_RUN_BUDGET_S", "180"))
HTTP_TIMEOUT_S = int(os.environ.get("BENCH_HTTP_TIMEOUT_S", "180"))

# ── Per-model HTTP timeout overrides (seconds) ───────────────────────────
# This is an unattended agent research loop: we are never in a rush and we
# care about *complete, gradable* answers, not speed. Slow CPU/MoE backends
# (notably `frontier` = Qwen3-Coder-480B on llama.cpp/blade, ~1-2 tok/s)
# need a far more generous ceiling than the fast vLLM lanes — otherwise the
# response is cut off mid-generation and the grader scores a
# truncated-but-correct answer 0, which poisons the model ranking.
#
# HOW TO TUNE (no code change needed — edit the runner CronJob):
#   • Set BENCH_MODEL_TIMEOUTS to a JSON object, e.g.
#       {"frontier": 1200, "long": 300, "code": 240}
#     It is merged over the defaults below, so you only list overrides.
#   • Any model not listed falls back to BENCH_HTTP_TIMEOUT_S.
#   • CRITICAL: keep the job's activeDeadlineSeconds (cronjobs.yaml) greater
#     than BENCH_RUN_BUDGET_S + the largest per-model timeout, or Kubernetes
#     kills the pod mid-run and you are back to truncation.
#   • If a run finishes with finish_reason == "length" it hit max_tokens,
#     not the clock — raise the prompt's max_tokens, not the timeout.
DEFAULT_MODEL_TIMEOUTS: dict[str, int] = {"frontier": 900, "long": 300}
try:
    MODEL_TIMEOUTS = {
        **DEFAULT_MODEL_TIMEOUTS,
        **{k: int(v) for k, v in json.loads(
            os.environ.get("BENCH_MODEL_TIMEOUTS", "{}")
        ).items()},
    }
except (ValueError, TypeError):
    MODEL_TIMEOUTS = dict(DEFAULT_MODEL_TIMEOUTS)


def model_timeout(model: str) -> int:
    """Per-model HTTP read timeout; falls back to the global default."""
    return int(MODEL_TIMEOUTS.get(model, HTTP_TIMEOUT_S))


# ── Multi-dimensional scoring + LLM-as-judge ─────────────────────────────
# Design + rationale: homelab/scripts/bench/EVAL-ARCHITECTURE.md.
#
# Every run is scored on several axes in [0,1] and blended into a
# composite the proposer optimises against — a truncated/refused/partial
# answer still gets a graded, comparable score, never a silent 0.
#
#   correctness  deterministic grader (regex/jsonpath/exact/length) where one
#                exists; the judge's correctness read for open-ended prompts.
#   quality      the cross-family judge (reasoning soundness/completeness).
#   latency      measured wall_ms, monotone-decreasing transform.
#   cost         output token efficiency (tiebreaker only).
#
# `format` is presently enforced *inside* the deterministic correctness
# graders (jsonpath/regex already check output shape); it is split into its
# own axis in a later phase.
# The judge is called DIRECTLY at its backend (the Mac MLX server), NOT through
# the LiteLLM router. Two reasons: (1) the router echoes the alias name in the
# response `model` field, so the cross-family integrity check below can't see
# the real backend; (2) far more important, the router's default_fallbacks would
# silently reroute a failed judge call onto a Qwen/Gemma *candidate* lane — the
# exact self-preference contamination the dedicated judge exists to remove.
# Calling the backend directly means a judge outage just fails the call (→
# deterministic-only degrade) instead of quietly substituting a biased judge.
# BENCH_JUDGE_BASE defaults to the LiteLLM base for back-compat; the CronJob
# points it straight at mac-short-judge.inference.svc.
JUDGE_BASE = os.environ.get("BENCH_JUDGE_BASE", LITELLM_BASE).rstrip("/")
# Backend model id. Direct to the Mac, this MUST be the exact id mlx_lm.server
# loaded (it 404s on anything else); the integrity guard then matches it.
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "judge")
# Substring the judge's returned model id MUST contain. The judge is pinned
# and cross-family; if LiteLLM silently reroutes a judge call onto a
# candidate lane (e.g. a fallback to chat/long), the returned id won't match
# and we discard the score rather than trust a self-preference-biased answer.
JUDGE_EXPECT = os.environ.get("BENCH_JUDGE_EXPECT", "llama").lower()
JUDGE_TIMEOUT_S = int(os.environ.get("BENCH_JUDGE_TIMEOUT_S", "120"))
JUDGE_MAX_TOKENS = int(os.environ.get("BENCH_JUDGE_MAX_TOKENS", "300"))
# When to spend a judge call: "all" = every run, "open_ended" = only prompts
# where a regex can't see quality (judge grader + open-ended classes),
# "off" = deterministic only. Keeps load modest on the M1 judge.
JUDGE_MODE = os.environ.get("BENCH_JUDGE_MODE", "open_ended").lower()
OPEN_ENDED_CLASSES = {"summarize", "plan", "reason", "safety"}

# Composite weight vectors per prompt class (correctness, quality, latency,
# cost). Overridable via BENCH_SCORE_WEIGHTS (JSON: class -> [c,q,l,co]).
# Weights are renormalised at compose time over whichever axes are present,
# so a missing judge (quality) simply reweights correctness/latency/cost.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "summarize": {"correctness": 0.25, "quality": 0.60, "latency": 0.075, "cost": 0.075},
    "plan":      {"correctness": 0.30, "quality": 0.55, "latency": 0.075, "cost": 0.075},
    "safety":    {"correctness": 0.45, "quality": 0.45, "latency": 0.05, "cost": 0.05},
    "reason":    {"correctness": 0.55, "quality": 0.35, "latency": 0.05, "cost": 0.05},
    "code":      {"correctness": 0.70, "quality": 0.20, "latency": 0.05, "cost": 0.05},
    "recall":    {"correctness": 0.75, "quality": 0.15, "latency": 0.05, "cost": 0.05},
    "format":    {"correctness": 0.75, "quality": 0.15, "latency": 0.05, "cost": 0.05},
}
DEFAULT_WEIGHT = {"correctness": 0.55, "quality": 0.30, "latency": 0.075, "cost": 0.075}
try:
    _W = json.loads(os.environ.get("BENCH_SCORE_WEIGHTS", "{}"))
    WEIGHTS = {**DEFAULT_WEIGHTS, **{k: dict(zip(("correctness", "quality", "latency", "cost"), v)) for k, v in _W.items()}}
except (ValueError, TypeError):
    WEIGHTS = dict(DEFAULT_WEIGHTS)

JUDGE_SYSTEM = (
    "You are a strict, impartial evaluator scoring an assistant's response to a "
    "task. Judge substance only: correctness, completeness, and whether it obeyed "
    "the task's explicit output instructions. Explicitly IGNORE response length, "
    "verbosity, and writing style — a concise correct answer must not score lower "
    "than a long one. Output one sentence of reasoning, then a final line that is "
    "exactly 'SCORE: N' where N is an integer 0-10."
)
_SCORE_RE = re.compile(r"score\s*[:=]\s*(\d+(?:\.\d+)?)", re.I)

# ── Phase 3: order-randomised pairwise judging ───────────────────────────
# For close matchups the proposer queues an A-vs-B comparison. The judge is
# asked TWICE with the two answers in swapped order; averaging cancels its
# position bias (the tendency to favour whichever answer it saw first).
JUDGE_PAIRWISE_SYSTEM = (
    "You are a strict, impartial evaluator comparing two assistant responses "
    "(FIRST and SECOND) to the same task. Judge substance only: correctness, "
    "completeness, and obedience to the task's explicit output instructions. "
    "Explicitly IGNORE response length, verbosity, ordering, and style — do not "
    "favour a response for being longer or for appearing first.  Output one sentence of reasoning, "
    "then a final line that is exactly 'WINNER: X' where X is FIRST, SECOND, or TIE."
)
_WINNER_RE = re.compile(r"winner\s*[:=]\s*(first|second|tie)", re.I)
PAIRWISE_MAX_TOKENS = int(os.environ.get("BENCH_PAIRWISE_MAX_TOKENS", "300"))

# ── Phase 8: optional Langfuse export (env-gated, best-effort) ────────────
# When LANGFUSE_HOST + keys are set, every scored run is mirrored to Langfuse
# as a trace + per-axis scores for trend/drift dashboards. Entirely optional:
# absent config or any error is swallowed so it can never affect a run.
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_ENABLED = bool(LANGFUSE_HOST and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def should_judge(grader_kind: str, prompt_class: str) -> bool:
    if JUDGE_MODE == "off":
        return False
    if JUDGE_MODE == "all":
        return True
    return grader_kind == "judge" or prompt_class in OPEN_ENDED_CLASSES


def judge_quality(
    prompt: dict[str, Any], answer: str
) -> tuple[float | None, str | None, str | None]:
    """Score answer quality [0,1] via the pinned cross-family judge.

    Returns (score, rationale, judge_model_returned). Returns (None, ...) when
    the judge is unavailable OR LiteLLM rerouted the call off the pinned judge
    (returned model id missing JUDGE_EXPECT) — the caller then degrades to
    deterministic-only scoring instead of trusting a biased answer.
    """
    task = prompt["user_text"]
    if prompt.get("system_text"):
        task = f"[system] {prompt['system_text']}\n\n{task}"
    user = (
        f"TASK:\n{task[:6000]}\n\n"
        f"ASSISTANT RESPONSE:\n{(answer or '(empty response)')[:6000]}\n\n"
        "Score the response now."
    )
    body = {
        "model": JUDGE_MODEL,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": JUDGE_MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=JUDGE_TIMEOUT_S) as client:
            resp = client.post(
                f"{JUDGE_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LITELLM_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if resp.status_code != 200:
            print(f"  judge unavailable: {resp.status_code}", flush=True)
            return None, None, None
        data = resp.json()
        returned = (data.get("model") or "").lower()
        if JUDGE_EXPECT and JUDGE_EXPECT not in returned:
            # LiteLLM rerouted off the pinned judge (fallback to a candidate).
            print(f"  judge rerouted to {returned!r}; discarding (integrity guard)", flush=True)
            return None, None, returned
        text = data["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001 — judge failure must never fail the run
        print(f"  judge error: {e}", flush=True)
        return None, None, None

    m = _SCORE_RE.search(text)
    if not m:
        print("  judge produced no parseable SCORE; discarding", flush=True)
        return None, text.strip()[:1000], returned
    score = _clamp01(float(m.group(1)) / 10.0)
    rationale = _SCORE_RE.sub("", text).strip()[:1000]
    return score, rationale, returned


def judge_pairwise(
    prompt: dict[str, Any], answer_first: str, answer_second: str
) -> tuple[float | None, str | None, str | None]:
    """Compare two answers to the same task; return preference for the FIRST.

    Returns (first_pref, rationale, judge_model_returned) where first_pref is
    1.0 if FIRST is judged better, 0.0 if SECOND, 0.5 for a tie. Returns
    (None, ...) on judge outage or an integrity-guard failure, exactly like the
    pointwise judge — the caller then records no preference rather than a biased
    one. The caller swaps the order across two calls and averages to cancel the
    judge's position bias.
    """
    task = prompt["user_text"]
    if prompt.get("system_text"):
        task = f"[system] {prompt['system_text']}\n\n{task}"
    user = (
        f"TASK:\n{task[:5000]}\n\n"
        f"RESPONSE FIRST:\n{(answer_first or '(empty response)')[:5000]}\n\n"
        f"RESPONSE SECOND:\n{(answer_second or '(empty response)')[:5000]}\n\n"
        "Compare the two responses now."
    )
    body = {
        "model": JUDGE_MODEL,
        "messages": [
            {"role": "system", "content": JUDGE_PAIRWISE_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": PAIRWISE_MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=JUDGE_TIMEOUT_S) as client:
            resp = client.post(
                f"{JUDGE_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LITELLM_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if resp.status_code != 200:
            print(f"  pairwise judge unavailable: {resp.status_code}", flush=True)
            return None, None, None
        data = resp.json()
        returned = (data.get("model") or "").lower()
        if JUDGE_EXPECT and JUDGE_EXPECT not in returned:
            print(f"  pairwise judge rerouted to {returned!r}; discarding", flush=True)
            return None, None, returned
        text = data["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001 — judge failure must never fail the run
        print(f"  pairwise judge error: {e}", flush=True)
        return None, None, None

    m = _WINNER_RE.search(text)
    if not m:
        print("  pairwise judge produced no parseable WINNER; discarding", flush=True)
        return None, text.strip()[:1000], returned
    verdict = m.group(1).lower()
    first_pref = {"first": 1.0, "second": 0.0, "tie": 0.5}[verdict]
    rationale = _WINNER_RE.sub("", text).strip()[:1000]
    return first_pref, rationale, returned


def langfuse_export(
    exp: dict[str, Any],
    prompt: dict[str, Any],
    axes: dict[str, float],
    composite: float | None,
    judge_score: float | None,
    judge_rationale: str | None,
    metrics: dict[str, Any],
) -> None:
    """Best-effort mirror of one scored run to Langfuse (trace + scores).

    No-op unless LANGFUSE_* env is set. Any failure is swallowed: observability
    export must never affect or fail a benchmark run.
    """
    if not LANGFUSE_ENABLED:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        trace_id = str(exp["id"])
        batch: list[dict[str, Any]] = [{
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "timestamp": now,
            "body": {
                "id": trace_id,
                "name": f"bench/{prompt['class']}/{exp['prompt_id']}",
                "input": prompt["user_text"][:2000],
                "output": (exp.get("result_text") or "")[:2000],
                "metadata": {
                    "model": exp["model"],
                    "prompt_id": exp["prompt_id"],
                    "proposed_by": exp.get("proposed_by"),
                    "wall_ms": metrics.get("wall_ms"),
                    "output_tokens": metrics.get("output_tokens"),
                    "judge_model": JUDGE_MODEL if judge_score is not None else None,
                    "judge_rationale": judge_rationale,
                },
            },
        }]
        scores: dict[str, float | None] = {**axes, "composite": composite, "judge": judge_score}
        for name, value in scores.items():
            if value is None:
                continue
            batch.append({
                "id": str(uuid.uuid4()),
                "type": "score-create",
                "timestamp": now,
                "body": {
                    "traceId": trace_id,
                    "name": name,
                    "value": float(value),
                    "dataType": "NUMERIC",
                },
            })
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{LANGFUSE_HOST}/api/public/ingestion",
                auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
                json={"batch": batch},
            )
    except Exception as e:  # noqa: BLE001 — observability is never load-bearing
        print(f"  langfuse export skipped: {e}", flush=True)


def latency_score(wall_ms: int | None) -> float:
    """Monotone-decreasing in wall time. 0ms→1, ~60s→0.37. Tiebreaker."""
    import math
    if not wall_ms:
        return 1.0
    return _clamp01(math.exp(-wall_ms / 60000.0))


def cost_score(output_tokens: int | None) -> float:
    """Token-efficiency proxy. Fewer tokens → higher. Tiebreaker only."""
    import math
    if not output_tokens:
        return 1.0
    return _clamp01(math.exp(-output_tokens / 800.0))


def compose(prompt_class: str, axes: dict[str, float]) -> float:
    """Weighted blend over whichever axes are present, renormalised."""
    w = WEIGHTS.get(prompt_class, DEFAULT_WEIGHT)
    num = sum(w.get(k, 0.0) * v for k, v in axes.items())
    den = sum(w.get(k, 0.0) for k in axes)
    return round(num / den, 4) if den else 0.0


def claim_one(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Pop a single queued experiment, mark it running, return its row.

    SKIP LOCKED keeps multiple runner pods from racing on the same row.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH next AS (
                SELECT id FROM bench.experiments
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE bench.experiments e
               SET status = 'running',
                   started_at = now(),
                   runner_pod = %s
              FROM next
             WHERE e.id = next.id
         RETURNING e.*;
            """,
            (POD_NAME,),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def fetch_prompt(conn: psycopg.Connection, prompt_id: str) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM bench.prompts WHERE id = %s", (prompt_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt {prompt_id!r} not found")
    return row


def call_model(
    model: str,
    params: dict[str, Any],
    prompt: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Single non-streaming chat completion. Returns (text, metrics)."""
    messages: list[dict[str, str]] = []
    if prompt["system_text"]:
        messages.append({"role": "system", "content": prompt["system_text"]})
    messages.append({"role": "user", "content": prompt["user_text"]})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": params.get("max_tokens", prompt["max_tokens"]),
        "temperature": params.get("temperature", 0.0),
        "stream": False,
    }
    for k in ("top_p", "presence_penalty", "frequency_penalty"):
        if k in params:
            body[k] = params[k]

    t0 = time.perf_counter()
    with httpx.Client(timeout=model_timeout(model)) as client:
        resp = client.post(
            f"{LITELLM_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {LITELLM_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    wall_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code != 200:
        raise RuntimeError(f"litellm {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}

    metrics: dict[str, Any] = {
        "wall_ms": wall_ms,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "model_returned": data.get("model"),
        "finish_reason": data["choices"][0].get("finish_reason"),
    }
    if metrics["output_tokens"]:
        metrics["tokens_per_sec"] = round(
            metrics["output_tokens"] / max(wall_ms / 1000.0, 0.001), 2
        )
    return text, metrics


def grade(text: str, kind: str, spec: dict[str, Any] | None) -> float:
    """Auto-grade the response. Returns score in [0, 1]."""
    spec = spec or {}
    if kind == "none":
        return 0.0
    if kind == "exact":
        return 1.0 if text.strip() == spec.get("equals", "") else 0.0
    if kind == "length":
        n = len(text)
        lo, hi = spec.get("min", 0), spec.get("max", 10**9)
        return 1.0 if lo <= n <= hi else 0.0
    if kind == "regex":
        flags = 0
        for ch in spec.get("flags", ""):
            flags |= {"i": re.I, "m": re.M, "s": re.S}.get(ch, 0)
        return 1.0 if re.search(spec["pattern"], text, flags) else 0.0
    if kind == "jsonpath":
        try:
            data = json.loads(text.strip().strip("`").lstrip("json").strip())
        except Exception:
            return 0.0
        ok = 0
        checks = spec.get("checks", [])
        for c in checks:
            try:
                # extremely small jsonpath subset: $.foo or $.foo.bar
                node: Any = data
                parts = c["path"].lstrip("$").lstrip(".").split(".") if c["path"] != "$" else []
                for p in parts:
                    node = node[p]
                if "equals" in c and node == c["equals"]:
                    ok += 1
                elif "contains" in c and isinstance(node, str) and c["contains"] in node:
                    ok += 1
            except Exception:
                pass
        return ok / max(len(checks), 1)
    if kind == "judge":
        return 0.0  # judge mode handled by a separate analyzer pass
    return 0.0


def finish_done(
    conn: psycopg.Connection,
    exp_id: str,
    text: str,
    metrics: dict[str, Any],
    correctness: float,
    scores: dict[str, float],
    composite: float,
    judge_score: float | None,
    judge_model: str | None,
    judge_rationale: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bench.experiments
               SET status = 'done',
                   finished_at = now(),
                   result_text = %s,
                   result_metrics = %s::jsonb,
                   grader_score = %s,
                   scores = %s::jsonb,
                   composite_score = %s,
                   judge_score = %s,
                   judge_model = %s,
                   judge_rationale = %s,
                   wall_ms = %s,
                   input_tokens = %s,
                   output_tokens = %s
             WHERE id = %s
            """,
            (
                text[:8000],
                json.dumps(metrics),
                correctness,
                json.dumps(scores),
                composite,
                judge_score,
                judge_model,
                judge_rationale,
                metrics.get("wall_ms"),
                metrics.get("input_tokens"),
                metrics.get("output_tokens"),
                exp_id,
            ),
        )
        conn.commit()


def finish_error(conn: psycopg.Connection, exp_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bench.experiments
               SET status = 'error', finished_at = now(), error = %s
             WHERE id = %s
            """,
            (error[:4000], exp_id),
        )
        conn.commit()


def claim_one_pairwise(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Pop a single queued pairwise comparison, mark it running, return its row."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH next AS (
                SELECT id FROM bench.pairwise
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE bench.pairwise pw
               SET status = 'running', started_at = now(), runner_pod = %s
              FROM next
             WHERE pw.id = next.id
         RETURNING pw.*;
            """,
            (POD_NAME,),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def finish_pairwise(
    conn: psycopg.Connection,
    pw_id: str,
    *,
    status: str,
    answer_a: str | None = None,
    answer_b: str | None = None,
    pref_ab: float | None = None,
    pref_ba: float | None = None,
    pref_a: float | None = None,
    position_bias: float | None = None,
    judge_model: str | None = None,
    rationale: str | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bench.pairwise
               SET status = %s, finished_at = now(),
                   answer_a = %s, answer_b = %s,
                   pref_ab = %s, pref_ba = %s, pref_a = %s, position_bias = %s,
                   judge_model = %s, rationale = %s, error = %s
             WHERE id = %s
            """,
            (
                status,
                answer_a[:8000] if answer_a else None,
                answer_b[:8000] if answer_b else None,
                pref_ab, pref_ba, pref_a, position_bias,
                judge_model, rationale, (error[:4000] if error else None),
                pw_id,
            ),
        )
        conn.commit()


def run_pairwise(conn: psycopg.Connection, pw: dict[str, Any]) -> None:
    """Execute one order-randomised pairwise comparison.

    Generates a fresh temp-0 answer for each model, asks the judge in BOTH
    orders, and stores the order-bias-cancelled preference for A plus the
    measured position bias. A judge outage on either order marks the row error
    (no preference recorded) rather than trusting a one-sided read.
    """
    prompt = fetch_prompt(conn, pw["prompt_id"])
    answer_a, _ = call_model(pw["model_a"], {"temperature": 0.0}, prompt)
    answer_b, _ = call_model(pw["model_b"], {"temperature": 0.0}, prompt)

    # Order 1: A first. first_pref is directly A's preference.
    p_ab, rat_ab, ret1 = judge_pairwise(prompt, answer_a, answer_b)
    # Order 2: B first. first_pref is B's preference; A's is its complement.
    p_ba_first, rat_ba, ret2 = judge_pairwise(prompt, answer_b, answer_a)

    if p_ab is None or p_ba_first is None:
        finish_pairwise(
            conn, pw["id"], status="error",
            answer_a=answer_a, answer_b=answer_b,
            judge_model=(ret1 or ret2),
            error="judge unavailable/rerouted on one or both orders",
        )
        print("  pairwise: judge unavailable on an order; recorded error", flush=True)
        return

    pref_ab = p_ab                 # A-preference, A shown first
    pref_ba = 1.0 - p_ba_first     # A-preference, A shown second
    pref_a = round((pref_ab + pref_ba) / 2.0, 4)
    position_bias = round(abs(pref_ab - pref_ba), 4)
    rationale = "\n---\n".join(r for r in (rat_ab, rat_ba) if r)[:1000]

    finish_pairwise(
        conn, pw["id"], status="done",
        answer_a=answer_a, answer_b=answer_b,
        pref_ab=pref_ab, pref_ba=pref_ba, pref_a=pref_a,
        position_bias=position_bias, judge_model=(ret1 or ret2),
        rationale=rationale,
    )
    if position_bias >= 0.5:
        emit_finding(
            conn, None, "judge_position_bias", "notable",
            f"{pw['model_a']} vs {pw['model_b']} on {pw['prompt_id']}: "
            f"order flipped the verdict (bias {position_bias:.2f})",
            {"prompt_id": pw["prompt_id"], "model_a": pw["model_a"],
             "model_b": pw["model_b"], "pref_ab": pref_ab, "pref_ba": pref_ba},
        )
    print(
        f"  pairwise {pw['model_a']} vs {pw['model_b']}: pref_a={pref_a} "
        f"position_bias={position_bias}",
        flush=True,
    )


def emit_finding(
    conn: psycopg.Connection,
    exp_id: str | None,
    kind: str,
    severity: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    """Insert a single bench.findings row for the daily digest."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bench.findings (kind, severity, summary, details, experiment_id)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            """,
            (kind, severity, summary, json.dumps(details), exp_id),
        )
        conn.commit()


def maybe_emit_finding(
    conn: psycopg.Connection,
    exp_id: str,
    model: str,
    prompt_id: str,
    composite: float,
    wall_ms: int,
    judge_score: float | None,
    judge_rationale: str | None,
) -> None:
    """Promote notable runs into bench.findings for the daily digest.

    Ranking now keys off the composite score (COALESCE to the deterministic
    grader_score for pre-Phase-1 rows). A low judge score carries its rationale
    into the finding so the digest shows *why* a model ranked where it did.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT avg(COALESCE(composite_score, grader_score))::float AS avg_score,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY wall_ms)::int AS p95
              FROM bench.experiments
             WHERE status = 'done'
               AND model = %s
               AND prompt_id = %s
               AND finished_at > now() - interval '7 days'
               AND id <> %s
            """,
            (model, prompt_id, exp_id),
        )
        prior = cur.fetchone() or {}

    findings: list[tuple[str, str, str, dict[str, Any]]] = []

    # A low-quality judged answer is itself a finding — the rationale is the
    # signal the binary loop used to throw away.
    if judge_score is not None and judge_score < 0.4:
        findings.append((
            "low_quality",
            "notable",
            f"{model}/{prompt_id}: judge {judge_score:.2f} — {(judge_rationale or '').splitlines()[0][:160]}",
            {"judge_score": judge_score, "rationale": judge_rationale},
        ))

    if prior.get("avg_score"):
        if prior["avg_score"] >= 0.7 and composite <= 0.3:
            findings.append((
                "accuracy_cliff",
                "important",
                f"{model}/{prompt_id}: dropped from {prior['avg_score']:.2f} avg to {composite:.2f}",
                {"prior_avg_composite": prior["avg_score"], "new_composite": composite,
                 "rationale": judge_rationale},
            ))
        if prior["p95"] and wall_ms > 1.5 * prior["p95"]:
            findings.append((
                "latency_regression",
                "notable",
                f"{model}/{prompt_id}: {wall_ms}ms vs p95 {prior['p95']}ms (+{wall_ms / prior['p95']:.1f}x)",
                {"prior_p95_ms": prior["p95"], "new_wall_ms": wall_ms},
            ))

    for kind, severity, summary, details in findings:
        emit_finding(conn, exp_id, kind, severity, summary, details)


def main() -> int:
    deadline = time.monotonic() + RUN_BUDGET_S
    ran = 0
    with psycopg.connect(DATABASE_URL, autocommit=False) as conn:
        while time.monotonic() < deadline:
            exp = claim_one(conn)
            if exp is None:
                if ran == 0:
                    print("queue empty", flush=True)
                break

            print(
                f"[{exp['id']}] model={exp['model']} prompt={exp['prompt_id']} "
                f"priority={exp['priority']} timeout={model_timeout(exp['model'])}s",
                flush=True,
            )
            try:
                prompt = fetch_prompt(conn, exp["prompt_id"])
                text, metrics = call_model(exp["model"], exp["params"] or {}, prompt)
                kind, pclass = prompt["grader_kind"], prompt["class"]

                # Deterministic correctness (None for judge-only prompts).
                det = None if kind == "judge" else grade(text, kind, prompt["grader_spec"])

                # Quality via the pinned cross-family judge, when warranted.
                judge_s = judge_rat = judge_ret = None
                if should_judge(kind, pclass):
                    judge_s, judge_rat, judge_ret = judge_quality(prompt, text)

                # Correctness axis: deterministic where it exists, else the judge.
                correctness = det if det is not None else judge_s

                axes: dict[str, float] = {
                    "latency": latency_score(metrics.get("wall_ms")),
                    "cost": cost_score(metrics.get("output_tokens")),
                }
                if correctness is not None:
                    axes["correctness"] = correctness
                if judge_s is not None:
                    axes["quality"] = judge_s

                # Composite only when we have a quality/correctness signal —
                # never fabricate a 0 for a judge-only prompt the judge couldn't
                # reach (that was the old poison-zero failure mode).
                gradable = correctness is not None or judge_s is not None
                composite = compose(pclass, axes) if gradable else None

                finish_done(
                    conn, exp["id"], text, metrics, correctness, axes, composite,
                    judge_s,
                    judge_ret if judge_s is not None else None,
                    judge_rat if judge_s is not None else None,
                )
                exp["result_text"] = text
                langfuse_export(exp, prompt, axes, composite, judge_s, judge_rat, metrics)
                if not gradable:
                    emit_finding(
                        conn, exp["id"], "judge_unavailable", "info",
                        f"{exp['model']}/{exp['prompt_id']}: judge-only prompt but "
                        f"judge unreachable; left ungraded (no poison-zero).",
                        {"judge_returned": judge_ret},
                    )
                else:
                    maybe_emit_finding(
                        conn, exp["id"], exp["model"], exp["prompt_id"],
                        composite, metrics["wall_ms"], judge_s, judge_rat,
                    )
                print(
                    f"  done composite={composite if composite is None else round(composite, 3)} "
                    f"correctness={correctness} judge={judge_s} "
                    f"wall={metrics['wall_ms']}ms tokens={metrics.get('output_tokens')}",
                    flush=True,
                )
            except Exception as e:
                tb = traceback.format_exc()
                finish_error(conn, exp["id"], f"{e}\n{tb}")
                print(f"  error: {e}", flush=True)
            ran += 1

        # Phase 3: drain queued pairwise comparisons with the remaining budget.
        # Single-runs are processed first (they feed the proposer's UCB stats);
        # pairwise is the more expensive confirm-lane signal, run second.
        pw_ran = 0
        while time.monotonic() < deadline:
            pw = claim_one_pairwise(conn)
            if pw is None:
                break
            print(
                f"[pairwise {pw['id']}] {pw['model_a']} vs {pw['model_b']} "
                f"prompt={pw['prompt_id']}",
                flush=True,
            )
            try:
                run_pairwise(conn, pw)
            except Exception as e:
                tb = traceback.format_exc()
                finish_pairwise(conn, pw["id"], status="error", error=f"{e}\n{tb}")
                print(f"  pairwise error: {e}", flush=True)
            pw_ran += 1

    print(
        f"runner finished, ran {ran} experiment(s), {pw_ran} pairwise", flush=True
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
