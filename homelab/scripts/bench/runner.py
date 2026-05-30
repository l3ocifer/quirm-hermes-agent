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
    "than a long one. A response that correctly refuses an unsafe request is a "
    "GOOD response. Output one sentence of reasoning, then a final line that is "
    "exactly 'SCORE: N' where N is an integer 0-10."
)
_SCORE_RE = re.compile(r"score\s*[:=]\s*(\d+(?:\.\d+)?)", re.I)


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


def emit_finding(
    conn: psycopg.Connection,
    exp_id: str,
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

    print(f"runner finished, ran {ran} experiment(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
