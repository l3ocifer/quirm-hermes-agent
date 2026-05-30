#!/usr/bin/env python3
"""Quirm benchmark proposer — Karpathy-style auto-research loop, brain half.

The runner (runner.py) executes whatever's queued. This script decides
*what to queue next* by reading bench.experiments and bench.prompts.

Three proposal sources, blended each tick:

  1. coverage  — every (model × prompt) pair should have at least
                 N=3 done runs. Queue any missing combos.
  2. confirm   — the top-3 (model, prompt) by avg score get extra
                 runs to tighten their stats (variance reduction).
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
PROPOSER_NAME = "proposer-v1"


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


def confirm_targets(conn: psycopg.Connection, top_n: int) -> list[dict[str, Any]]:
    """Top-N (model, prompt) by avg COMPOSITE score with low run count.

    Ranks on the multi-dimensional `composite_score` (correctness + quality +
    latency + cost), falling back to the deterministic `grader_score` only for
    rows graded before Phase 1. Ranking on composite — not the binary grader —
    is the point of the judge work: the loop spends its confirm budget on the
    models that win on the blended objective, not just on a regex pass.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT e.model,
                   e.prompt_id,
                   p.version AS prompt_version,
                   avg(COALESCE(e.composite_score, e.grader_score))::float AS avg_score,
                   count(*) AS runs
              FROM bench.experiments e
              JOIN bench.prompts p ON p.id = e.prompt_id
             WHERE e.status = 'done'
               AND e.finished_at > now() - interval '14 days'
             GROUP BY e.model, e.prompt_id, p.version
            HAVING count(*) >= %s
            ORDER BY avg_score DESC, runs ASC
             LIMIT %s;
            """,
            (MIN_RUNS_PER_PAIR, top_n),
        )
        return cur.fetchall()


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

        # 2. confirm — top-N pairs get an extra run, priority=80
        if len(rows) < room:
            for t in confirm_targets(conn, CONFIRM_TOP_N):
                if len(rows) >= room:
                    break
                rows.append({
                    "prompt_id": t["prompt_id"],
                    "prompt_version": t["prompt_version"],
                    "model": t["model"],
                    "params": json.dumps({"temperature": 0.0}),
                    "proposed_by": f"{PROPOSER_NAME}/confirm",
                    "priority": 80,
                })

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
        print(f"enqueued {inserted} experiment(s)", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
