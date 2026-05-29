# Quirm eval architecture — multi-dimensional scoring with a dedicated judge

Status: **in rollout** (Phase 1 schema landed; judge lane being stood up).
Owner: Quirm (Hermes agent). Consumed by the bench proposer/runner/analyzer loop.

This is the design for moving Quirm's auto-research loop off **binary pass/fail
grading** and onto **multi-dimensional scoring** with a **pinned, cross-family
LLM-as-judge**. It records *why* the design is shaped this way so the loop can
keep tuning itself without re-litigating the fundamentals.

---

## 1. Why change anything

The v1 loop graded every run with a single deterministic check (regex / jsonpath
/ exact) and stored one `grader_score ∈ {0, 1}`. Two failure modes fell out of
that in production:

1. **Correct answers scored 0.** A double-escaped regex, a model that refused
   *correctly* but phrased it differently, or a slow model truncated by an HTTP
   timeout all scored 0 — indistinguishable from a genuinely wrong answer. The
   ranking signal was poisoned by grader brittleness, not model quality.
2. **Binary throws away most of the signal.** Every response carries gradable
   information — partial correctness, formatting, reasoning quality, whether a
   refusal was well-justified. A 0/1 gate discards all of it, so the loop can't
   tell a near-miss from a non-answer, and can't rank two models that both
   "pass".

The user's framing: *"Isn't anything we get back from the LLM going to contain
useful information, even if it's the failure of the answer?"* — yes. The loop
should extract signal from **every** run, not gate on one brittle check.

---

## 2. Scoring model — every axis, every run

Each run is scored on multiple axes in `[0, 1]`, blended into a `composite_score`
the proposer optimises against. The deterministic checks become **one input
among several**, not the gate.

| Axis | Source | Notes |
|---|---|---|
| `correctness` | deterministic grader (regex/jsonpath/exact/length) **or** judge | The fast, free, objective check where one exists. Falls back to the judge when `grader_kind = judge` or the deterministic check is advisory. |
| `format` | deterministic (schema/shape checks) | Did it obey the output contract (JSON only, N lines, code-only)? |
| `quality` | **judge** | Reasoning soundness, completeness, clarity — the axis a regex can't see. |
| `latency` | measured `wall_ms` → normalised | Already collected. Normalised per prompt-class so a slow lane isn't punished for being a slow lane. |
| `cost` | tokens × per-model rate | Cheap models that match quality should rank up. |

`composite_score = Σ wᵢ·axisᵢ` with a weight vector per prompt-class (a code
prompt weights `correctness`+`format`; a summarise prompt weights `quality`).
Weights live in config so the loop can tune them without a schema change.

**Key principle:** a truncated, refused, or partially-correct answer still gets
a *graded, stored, comparable* score and a rationale — it is never silently 0.

---

## 3. The judge — dedicated, pinned, cross-family

LLM-as-judge is only trustworthy if it is **reproducible** and **unbiased**:

- **Pinned model + low temperature (≤0.3).** A judge whose identity changes run
  to run produces scores you can't calibrate or trend. So the judge is a
  *specific, stable* model — **not** "whatever inference lane happens to be
  free". This is the decisive reason the judge is a dedicated deployment.
- **Cross-family from the candidates.** Self-preference bias (a model scoring
  its own family higher) runs 10–25%. Candidates are Qwen (`long`, `frontier`,
  `code`, `mac`) and Gemma (`chat`), so the judge must be a **third family**.
  Initial judge: **Llama-3.1-8B-Instruct (4-bit MLX)** — proven MLX build,
  cross-family, cheap. Upgrade path: **Prometheus-2** (purpose-built rubric
  evaluator) once a clean MLX conversion is confirmed.
- **Reasoning before score.** The judge emits a short rationale *then* a score
  (G-Eval style); the rationale is stored and promoted to a `finding`, so a
  human can audit *why* a model was ranked where it was.

### Placement: `short` (Mac mini M1 / 16 GB)

The judge is an off-hot-path, steady, no-rush workload — exactly what the Mac
appliances are for. Of the two Macs:

- **3090s (`alef`/`thebeast`)** — ✗ judging would steal the GPUs serving real
  agent traffic.
- **`tall` (M2/24 GB)** — ✗ already serving the `mac` coder *candidate* (~17 GB)
  and doubles as the primary dev workstation; a persistent judge daemon would
  contend with both, and judging on the same box as a candidate is poor
  isolation.
- **`short` (M1/16 GB)** — ✅ headless appliance running only the lightweight
  BlueBubbles/iMessage relay (~4–6 GB), ~10 GB free. Isolated from every
  candidate, off the production GPUs, and the MLX serving pattern is already
  proven on `tall`.

`short` runs the iMessage relay every agent depends on, so the judge is
constrained to protect it: **small 4-bit model**, **`iogpu.wired_limit_mb`
capped**, **concurrency = 1**, and the judge only fires on the prompts that
need it (open-ended / `judge` grader / confirm lane) — not on every run. If
relay contention ever appears, the fallback is to displace `tall`'s coder
testbed (lower value than the judge the whole loop depends on).

### Wiring (mirrors the `tall` MLX pattern)

```
short:8081  mlx_lm.server (Llama-3.1-8B-Instruct-4bit)   LaunchDaemon com.homelab.mlx-judge
   │
   └── Service+Endpoints  mac-short-judge.inference.svc.cluster.local:8081   (17-mac-short-judge.yaml)
         │
         └── LiteLLM model group `judge-mac-short` → alias `judge`  (pinned, temperature 0.2, drop_params)
               │
               └── runner.py judge grader → judge_score + judge_rationale
```

The judge is exposed **only** via the explicit `judge` alias — no public alias,
no fallback ladder targets it (a one-way leaf, like `mac`).

---

## 4. Bias controls (Phase 3)

- **Pointwise with rubric** for the per-run quality axis (cheap, scales).
- **Pairwise, order-randomised** for the confirm lane: when two models are close
  on composite, run A-vs-B *and* B-vs-A and average, to cancel position bias.
- **No length leakage:** the rubric instructs the judge to ignore verbosity;
  cost/latency axes already penalise needless length separately.

---

## 5. Adaptive budget (Phase 4)

The loop has finite GPU/Mac time, so the proposer should spend it where it buys
the most ranking information:

- **UCB arm selection:** treat (model × prompt-class) as bandit arms; pull the
  arms whose ranking is most uncertain, not a uniform sweep.
- **Bayesian sequential stopping:** stop sampling a (model, prompt) pair once the
  score's credible interval is tight enough to rank it confidently; reallocate
  the saved runs to contested pairs.

---

## 6. Calibration loop (Phase 8)

A judge you don't calibrate is a judge you can't trust:

- Sample N judged runs/week for a human spot-check; compute **Cohen's κ**
  between human and judge and surface it in the daily digest.
- Push per-axis scores + rationale to **Langfuse** for trend/drift visibility.
- If κ drifts, re-pin or re-prompt the judge — never silently let it move.

---

## 7. Rollout phases

| Phase | Scope | State |
|---|---|---|
| 1 | Multi-dim score schema (`scores` jsonb, `judge_*`, `composite_score`) + views | **landing** |
| 2 | Judge lane: MLX on `short` + LiteLLM `judge` alias + runner judge grader | next |
| 3 | Bias controls: order-randomised pairwise for confirm lane | pending |
| 4 | Adaptive budget: UCB arms + Bayesian sequential stopping | pending |
| 8 | Calibration: human spot-check, Cohen's κ in digest, Langfuse export | pending |

Schema changes are **additive** (`ADD COLUMN IF NOT EXISTS`); the deterministic
`grader_score` stays populated for backward-compatible dashboards and as the
`correctness` axis input.
