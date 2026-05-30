# Quirm eval architecture — multi-dimensional scoring with a dedicated judge

Status: **live** — Phases 1 & 2 deployed and verified end-to-end (composite +
judge scores landing with the correct backend `judge_model` and rationales;
deterministic grader regex corrected so right answers no longer score 0).
Phases 3, 4, 8 pending.
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
   └── Service+Endpoints  mac-short-judge.inference.svc.cluster.local:8081   (18-mac-short-judge.yaml)
         │
         ├── runner.py judge grader (PRIMARY)  →  judge_score + judge_rationale
         │     calls the Service DIRECTLY (BENCH_JUDGE_BASE), NOT via LiteLLM
         │
         └── LiteLLM model group `judge-mac-short` → alias `judge`  (manual/ad-hoc use only;
               pinned, temperature 0.2, drop_params)
```

**Why the runner bypasses the LiteLLM router for judging.** If the judge call
went through LiteLLM, a judge outage could be silently rerouted by a fallback
ladder onto a *candidate* lane — the model judging its own family, the exact
self-preference bias we are trying to eliminate, and invisibly. Worse, LiteLLM
returns the *alias* (`model=judge`) not the backend id, so the integrity guard
couldn't tell who actually answered. So the runner calls the Mac Service
directly at `BENCH_JUDGE_BASE` and asserts the returned model id contains
`BENCH_JUDGE_EXPECT` (`llama`); anything else is discarded and the run is left
judge-unscored rather than contaminated. The LiteLLM `judge` alias remains for
manual probing only — no public alias, no fallback ladder targets it.

> **Off-cluster Endpoints caveat:** ArgoCD's `argocd-cm` excludes
> `Endpoints`/`EndpointSlice` from management, so `18-mac-short-judge.yaml`'s
> Endpoints object must be **`kubectl apply`-ed once by hand** — the Service
> alone resolves to nothing. The Macs are not cluster members; this manual
> Endpoints is the only bridge.

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
| 1 | Multi-dim score schema (`scores` jsonb, `judge_*`, `composite_score`) + views | **done** |
| 2 | Judge lane: MLX on `short` + direct runner judge grader + integrity guard | **done** |
| 3 | Bias controls: order-randomised pairwise for confirm lane | pending |
| 4 | Adaptive budget: UCB arms + Bayesian sequential stopping | pending |
| 8 | Calibration: judge↔grader agreement + Cohen's κ in digest, Langfuse export | pending |

Schema changes are **additive** (`ADD COLUMN IF NOT EXISTS`); the deterministic
`grader_score` stays populated for backward-compatible dashboards and as the
`correctness` axis input.

---

## 8. Operational hardening (lessons from the Phase 1–2 rollout)

These bit us in production; the fixes are in the manifests/seed, but the
*reasons* live here so they are not re-introduced.

1. **Pin bench jobs to amd64.** `schema-job.yaml` and both bench CronJobs
   pull images and `pip install` at startup. When the scheduler placed them on
   an arm64 Pi (`top`/`bottom`/`hailo`) — whose DNS intermittently fails
   `lookup registry-1.docker.io` — the pod wedged in `ImagePullBackOff`. For
   the schema *Sync hook* that silently blocked the **entire** ArgoCD sync, so
   no code or seed update could land. All three carry
   `nodeSelector: kubernetes.io/arch: amd64` (nodes: alef, thebeast, blade).

2. **The seed SQL is a quoted heredoc — mind two escaping traps:**
   - **Backslash:** with `standard_conforming_strings=on` a backslash is
     literal in SQL, so a regex metaclass is written **once** (`\b` → stored
     `\b` → Python word boundary). Doubling it to `\\b` stores a literal
     backslash and the grader matches nothing — correct answers score 0.
   - **Apostrophe:** a literal `'` in a pattern (e.g. `can't`) is a SQL string
     delimiter and must be **doubled** (`''`). Shell-style `'\''` is verbatim
     inside the quoted heredoc and silently terminates the SQL literal,
     aborting the whole seed.
   Validate the seed before relying on a deploy:
   `awk` the SQL out of the manifest and run it wrapped in `BEGIN; … ROLLBACK;`
   against the DB with `-v ON_ERROR_STOP=1` — it parses everything and changes
   nothing.

3. **View columns can't be reordered with `CREATE OR REPLACE`.** Adding the
   Phase-1 columns mid-list errored (`cannot change name of view column`). The
   seed `DROP VIEW IF EXISTS` then `CREATE` for `recent_done`/`model_summary`.

4. **The grader runs in Python `re`, not Postgres.** `\b` is a word boundary in
   Python but a backspace in Postgres ARE (which uses `\y`). Validate grader
   patterns with Python, not a `~*` query, or you get false negatives.

5. **Recovering a wedged ArgoCD hook (no CLI).** A hook stuck Running blocks the
   op; clearing its `argocd.argoproj.io/hook-finalizer` by hand can leave the op
   tracking a vanished job. Terminate Argo-natively via the CRD:
   `kubectl -n argocd patch application quirm --type merge -p
   '{"status":{"operationState":{"phase":"Terminating"}}}'` (the Application
   has no status subresource, so a plain merge updates status), then re-trigger
   by patching `.operation` with the target `sync.revision`.

> **Historical-data note:** runs graded *before* the regex fix carry false
> `grader_score = 0`. Their `composite_score` was still computed (from the other
> axes), but model rankings that lean on pre-fix `correctness` are biased low.
> Treat the fix commit as the calibration epoch; Phase 8's κ check should ignore
> rows older than it.
