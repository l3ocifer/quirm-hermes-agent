#!/usr/bin/env python3
"""Quirm benchmark runner — pops one queued experiment and runs it.

Karpathy-style auto-research loop, runner half:

  while queue not empty:
      exp  = SELECT next experiment FOR UPDATE SKIP LOCKED
      mark exp as running
      try:
          result, metrics = call_litellm(model, params, prompt)
          score = grade(result, prompt.grader_kind, prompt.grader_spec)
          mark exp done with result + metrics + score
      except Exception as e:
          mark exp error with traceback
      maybe emit a finding if (regression | new pareto-frontier | accuracy cliff)

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
    score: float,
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
                   wall_ms = %s,
                   input_tokens = %s,
                   output_tokens = %s
             WHERE id = %s
            """,
            (
                text[:8000],
                json.dumps(metrics),
                score,
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


def maybe_emit_finding(
    conn: psycopg.Connection,
    exp_id: str,
    model: str,
    prompt_id: str,
    score: float,
    wall_ms: int,
) -> None:
    """Promote notable runs into bench.findings for the daily digest."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT avg(grader_score)::float AS avg_score,
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

    if not prior.get("avg_score"):
        return  # not enough history yet

    findings: list[tuple[str, str, str, dict[str, Any]]] = []
    if prior["avg_score"] >= 0.8 and score == 0.0:
        findings.append((
            "accuracy_cliff",
            "important",
            f"{model}/{prompt_id}: dropped from {prior['avg_score']:.2f} avg to 0.0",
            {"prior_avg_score": prior["avg_score"], "new_score": score},
        ))
    if prior["p95"] and wall_ms > 1.5 * prior["p95"]:
        findings.append((
            "latency_regression",
            "notable",
            f"{model}/{prompt_id}: {wall_ms}ms vs p95 {prior['p95']}ms (+{wall_ms / prior['p95']:.1f}x)",
            {"prior_p95_ms": prior["p95"], "new_wall_ms": wall_ms},
        ))

    if not findings:
        return

    with conn.cursor() as cur:
        for kind, severity, summary, details in findings:
            cur.execute(
                """
                INSERT INTO bench.findings (kind, severity, summary, details, experiment_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (kind, severity, summary, json.dumps(details), exp_id),
            )
        conn.commit()


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
                score = grade(text, prompt["grader_kind"], prompt["grader_spec"])
                finish_done(conn, exp["id"], text, metrics, score)
                maybe_emit_finding(
                    conn,
                    exp["id"],
                    exp["model"],
                    exp["prompt_id"],
                    score,
                    metrics["wall_ms"],
                )
                print(
                    f"  done score={score:.2f} wall={metrics['wall_ms']}ms "
                    f"tokens={metrics.get('output_tokens')}",
                    flush=True,
                )
            except Exception as e:
                tb = traceback.format_exc()
                finish_error(conn, exp["id"], f"{e}\n{tb}")
                print(f"  error: {e}", flush=True)
            ran += 1

    print(f"runner finished, ran {ran} experiment(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
