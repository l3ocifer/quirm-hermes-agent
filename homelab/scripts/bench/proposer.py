#!/usr/bin/env python3
"""Quirm benchmark proposer — Karpathy-style auto-research loop, brain half.

The runner (runner.py) executes whatever's queued. This script decides
*what to queue next* by reading bench.experiments and bench.prompts.

Proposal sources, blended each tick:

  1. coverage  — every (model × prompt) pair should have at least
                 N=3 done runs. Queue any missing combos (UCB bootstrap).
  2. confirm   — UCB1 over arms (Phase 4): exploit high mean composite,
                 explore high uncertainty. Arms that are SETTLED (tight CI
                 or hit the run cap) are skipped — Bayesian sequential
                 stopping — so the budget flows to the contested arms.
  2b. pairwise — order-randomised A-vs-B (Phase 3): for models that are
                 statistically close on a prompt, queue a head-to-head into
                 bench.pairwise so the runner can break the tie with a
                 position-bias-cancelled judge comparison.
  3. explore   — epsilon-greedy: with probability EXPLORE_PROB,
                 propose a perturbed experiment (different temperature,
                 max_tokens) to look for off-Pareto wins.

The model catalog comes from the bench.models table if present, else
from a hardcoded fallback that matches our LiteLLM aliases. Add new
models by inserting them into bench.models — the proposer picks them
up on the next tick. The whole loop is self-healing.

Designed to run as a Kubernetes CronJob every 30 minutes. It only ever
*adds* queued rows; it never deletes. Safety: it won't enqueue more
than MAX_BACKLOG rows total to keep the runner moving.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ["DATABASE_URL"]

# Fallback model catalog. Each entry: (alias, params, weight, tags).
# Weight is used by the explore branch to bias toward cheaper / faster
# models when picking perturbations (don't waste budget on frontier).
DEFAULT_MODELS: list[dict[str, Any]] = [
    {"alias": "chat", "weight": 1.0, "tags": ["fast", "default"]},
    {"alias": "long", "weight": 0.7, "tags": ["long-context"]},
    {"alias": "frontier", "weight": 0.3, "tags": ["expensive", "smart"]},
    {"alias": "code", "weight": 0.8, "tags": ["code"]},
]

MIN_RUNS_PER_PAIR = int(os.environ.get("BENCH_MIN_RUNS_PER_PAIR", "3"))
CONFIRM_TOP_N = int(os.environ.get("BENCH_CONFIRM_TOP_N", "3"))
EXPLORE_PROB = float(os.environ.get("BENCH_EXPLORE_PROB", "0.20"))
MAX_BACKLOG = int(os.environ.get("BENCH_MAX_BACKLOG", "120"))

# ── Phase 4: adaptive budget (UCB + Bayesian sequential stopping) ─────────
# An "arm" is a (model, prompt) pair. Once we are confident in an arm's score
# we STOP sampling it and pour the budget into contested arms.
#   MAX_RUNS_PER_PAIR  hard cap — never sample an arm beyond this.
#   CI_EPSILON         stop early once the 95% CI half-width of the composite
#                      mean is this tight (we can rank it confidently).
#   UCB_C              exploration weight; higher = explore uncertain arms more.
MAX_RUNS_PER_PAIR = int(os.environ.get("BENCH_MAX_RUNS_PER_PAIR", "12"))
CI_EPSILON = float(os.environ.get("BENCH_CI_EPSILON", "0.08"))
UCB_C = float(os.environ.get("BENCH_UCB_C", "0.7"))

# ── Phase 3: order-randomised pairwise confirm lane ──────────────────────
#   PAIRWISE_MARGIN  two models on a prompt are "close" (worth a head-to-head)
#                    when their mean composites differ by <= this.
#   PAIRWISE_TOP_N   max pairwise comparisons to enqueue per tick.
PAIRWISE_MARGIN = float(os.environ.get("BENCH_PAIRWISE_MARGIN", "0.06"))
PAIRWISE_TOP_N = int(os.environ.get("BENCH_PAIRWISE_TOP_N", "3"))
PROPOSER_NAME = "proposer-v2"


def list_models(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT to_regclass('bench.models') IS NOT NULL AS has_models;
            """
        )
        if cur.fetchone()["has_models"]:
            cur.execute("SELECT alias, weight, tags FROM bench.models WHERE enabled = true;")
            rows = cur.fetchall()
            if rows:
                return rows
    return DEFAULT_MODELS


def list_prompts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, version, tags FROM bench.prompts ORDER BY id;")
        return cur.fetchall()


def current_backlog(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bench.experiments WHERE status = 'queued';")
        return cur.fetchone()[0]


def coverage_gaps(
    conn: psycopg.Connection,
    models: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return (model, prompt) pairs with fewer than MIN_RUNS_PER_PAIR done runs."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT model, prompt_id, count(*) AS done_runs
              FROM bench.experiments
             WHERE status = 'done'
             GROUP BY model, prompt_id;
            """
        )
        existing = {(r["model"], r["prompt_id"]): r["done_runs"] for r in cur.fetchall()}

    gaps: list[dict[str, Any]] = []
    for m in models:
        for p in prompts:
            need = MIN_RUNS_PER_PAIR - existing.get((m["alias"], p["id"]), 0)
            if need > 0:
                gaps.append({
                    "model": m["alias"],
                    "prompt_id": p["id"],
                    "prompt_version": p["version"],
                    "need": need,
                })
    return gaps


def read_arm_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-arm composite stats + run cap, joined to prompt version.

    Reads the bench.arm_stats view (mean, stddev, 95% CI half-width per
    (model, prompt)) so UCB selection and sequential stopping share one
    definition with the digest.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.model, a.prompt_id, p.version AS prompt_version, a.class,
                   a.n_runs, a.mean_composite, a.ci_halfwidth
              FROM bench.arm_stats a
              JOIN bench.prompts p ON p.id = a.prompt_id;
            """
        )
        return cur.fetchall()


def is_settled(arm: dict[str, Any]) -> bool:
    """Sequential stopping: stop sampling an arm once it is confidently ranked.

    Settled when we hit the hard run cap, OR the composite mean's 95% CI is
    tight enough (and we have the minimum bootstrap runs) to rank it.
    """
    n = arm["n_runs"]
    if n >= MAX_RUNS_PER_PAIR:
        return True
    return n >= MIN_RUNS_PER_PAIR and (arm["ci_halfwidth"] or 0.0) <= CI_EPSILON


def ucb_targets(arms: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """UCB1 over unsettled arms — exploit high mean, explore high uncertainty.

    score = mean + UCB_C * sqrt(ln(N) / n), N = total runs across arms. Returns
    the top-N unsettled arms (already past the bootstrap min) to sample once more.
    Settled arms are skipped — that freed budget is what goes to contested arms.
    """
    eligible = [a for a in arms if a["n_runs"] >= MIN_RUNS_PER_PAIR and not is_settled(a)]
    total = max(sum(a["n_runs"] for a in arms), 1)
    ln_n = math.log(total) if total > 1 else 1.0
    for a in eligible:
        mean = a["mean_composite"] or 0.0
        a["ucb"] = mean + UCB_C * math.sqrt(ln_n / max(a["n_runs"], 1))
    eligible.sort(key=lambda a: a["ucb"], reverse=True)
    return eligible[:top_n]


def pairwise_candidates(arms: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Close (model_a, model_b) matchups per prompt worth a head-to-head.

    Within each prompt, sort models by mean composite and emit adjacent pairs
    whose means differ by <= PAIRWISE_MARGIN — the contests a pointwise score
    can't separate. Models are stored sorted so the pair dedupes regardless of
    which ranked higher.
    """
    by_prompt: dict[str, list[dict[str, Any]]] = {}
    for a in arms:
        if a["n_runs"] >= MIN_RUNS_PER_PAIR and a["mean_composite"] is not None:
            by_prompt.setdefault(a["prompt_id"], []).append(a)

    out: list[dict[str, Any]] = []
    for prompt_id, group in by_prompt.items():
        group.sort(key=lambda a: a["mean_composite"], reverse=True)
        for hi, lo in zip(group, group[1:]):
            if hi["mean_composite"] - lo["mean_composite"] <= PAIRWISE_MARGIN:
                ma, mb = sorted((hi["model"], lo["model"]))
                out.append({
                    "prompt_id": prompt_id,
                    "prompt_version": hi["prompt_version"],
                    "model_a": ma,
                    "model_b": mb,
                    "gap": hi["mean_composite"] - lo["mean_composite"],
                })
    out.sort(key=lambda c: c["gap"])  # closest contests first
    return out[:limit]


def pending_pairwise(conn: psycopg.Connection) -> set[tuple[str, str, str]]:
    """(prompt_id, model_a, model_b) already queued or recently compared — skip."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT prompt_id, model_a, model_b FROM bench.pairwise
             WHERE status = 'queued'
                OR (status = 'done' AND finished_at > now() - interval '1 day');
            """
        )
        return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def insert_pairwise(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO bench.pairwise
                (prompt_id, prompt_version, model_a, model_b, proposed_by, priority)
            VALUES (%(prompt_id)s, %(prompt_version)s, %(model_a)s, %(model_b)s,
                    %(proposed_by)s, %(priority)s);
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def insert_experiments(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO bench.experiments
                (prompt_id, prompt_version, model, params, proposed_by, priority)
            VALUES (%(prompt_id)s, %(prompt_version)s, %(model)s,
                    %(params)s::jsonb, %(proposed_by)s, %(priority)s);
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def main() -> int:
    inserted = 0
    with psycopg.connect(DATABASE_URL, autocommit=False) as conn:
        backlog = current_backlog(conn)
        room = max(MAX_BACKLOG - backlog, 0)
        print(f"backlog={backlog} room={room}", flush=True)
        if room <= 0:
            print("backlog at MAX, skipping", flush=True)
            return 0

        models = list_models(conn)
        prompts = list_prompts(conn)
        if not models or not prompts:
            print("no models or no prompts", flush=True)
            return 0

        rows: list[dict[str, Any]] = []

        # 1. coverage — fill gaps first, priority=50
        for gap in coverage_gaps(conn, models, prompts):
            for _ in range(gap["need"]):
                if len(rows) >= room:
                    break
                rows.append({
                    "prompt_id": gap["prompt_id"],
                    "prompt_version": gap["prompt_version"],
                    "model": gap["model"],
                    "params": json.dumps({"temperature": 0.0}),
                    "proposed_by": f"{PROPOSER_NAME}/coverage",
                    "priority": 50,
                })
            if len(rows) >= room:
                break

        # 2. confirm — UCB1 over unsettled arms (Phase 4), priority=80.
        # Settled arms (tight CI or run cap) are skipped; that budget flows to
        # the contested arms UCB surfaces. Replaces the old "top-N by score".
        arms = read_arm_stats(conn)
        if len(rows) < room:
            for t in ucb_targets(arms, CONFIRM_TOP_N):
                if len(rows) >= room:
                    break
                rows.append({
                    "prompt_id": t["prompt_id"],
                    "prompt_version": t["prompt_version"],
                    "model": t["model"],
                    "params": json.dumps({"temperature": 0.0}),
                    "proposed_by": f"{PROPOSER_NAME}/confirm-ucb",
                    "priority": 80,
                })

        # 2b. pairwise — order-randomised head-to-heads for close matchups
        # (Phase 3). Separate bench.pairwise queue, deduped against pending.
        pending = pending_pairwise(conn)
        pw_rows = [
            {
                "prompt_id": c["prompt_id"],
                "prompt_version": c["prompt_version"],
                "model_a": c["model_a"],
                "model_b": c["model_b"],
                "proposed_by": f"{PROPOSER_NAME}/pairwise",
                "priority": 90,
            }
            for c in pairwise_candidates(arms, PAIRWISE_TOP_N)
            if (c["prompt_id"], c["model_a"], c["model_b"]) not in pending
        ]
        pw_inserted = insert_pairwise(conn, pw_rows)

        # 3. explore — epsilon-greedy perturbations, priority=120
        if len(rows) < room:
            for _ in range(min(5, room - len(rows))):
                if random.random() > EXPLORE_PROB:
                    continue
                m = random.choices(models, weights=[x["weight"] for x in models])[0]
                p = random.choice(prompts)
                params = {
                    "temperature": random.choice([0.3, 0.7]),
                    "max_tokens": random.choice([512, 2048]),
                }
                rows.append({
                    "prompt_id": p["id"],
                    "prompt_version": p["version"],
                    "model": m["alias"],
                    "params": json.dumps(params),
                    "proposed_by": f"{PROPOSER_NAME}/explore",
                    "priority": 120,
                })

        inserted = insert_experiments(conn, rows)
        print(
            f"enqueued {inserted} experiment(s), {pw_inserted} pairwise", flush=True
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
