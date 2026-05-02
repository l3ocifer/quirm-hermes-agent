# Quirm — tools and environment

## Runtime

- **Framework**: Hermes Agent (NousResearch/hermes-agent fork at
  `l3ocifer/quirm-hermes-agent`)
- **Image**: `ghcr.io/l3ocifer/quirm-hermes-agent:latest`
- **Namespace**: `agents-shared`
- **Schedule**: floats. Soft preference for `thebeast` (more RAM —
  benchmarking + prototyping benefits from headroom).
- **State PVC**: `quirm-state` (longhorn-single, 10 GiB — sessions,
  skills cache, eval-run results)
- **Graph PVCs mounted**:
  - `quirm-graph` RW — Quirm's eval results, methodology notes,
    prototype sketches, locked-tower entries
  - `leo-graph` (restricted-write paths only — `pages/agent-coordination/`,
    `pages/research-memos/`)
  - All six siblings' graphs RO — for cross-agent context when
    benchmarking

## Models

Quirm calls models via LiteLLM (`http://litellm.inference.svc.cluster.local:4000/v1`).
Configured aliases in `hermes.toml`:

| Alias | Use |
|---|---|
| `chat` | default conversational model |
| `code` | benchmark code-quality evals + prototype generation |
| `long` | long-context retrieval evals |
| `embed` | embedding model for memory-search benchmarks |
| `frontier` | gold-standard for grading rubric outputs |

Quirm does NOT pin to a specific model — that's the point of being
the benchmarker. Whatever LiteLLM routes to is what production
agents see, so what Quirm sees needs to be that.

## Communication channels

| Channel | Use |
|---|---|
| Matrix `@quirm:leopaska.xyz` | weekly digests + on-demand reports to Leo + Vetinari briefings |
| Telegram bot (shared) | morning benchmark summary at 06:00 ET |
| ntfy `ntfy.leopaska.xyz/quirm` | regression alerts (P95 latency > +20%, cost spikes) |
| A2A — peer to all 6 siblings | timed benchmark calls, cost-tagged via virtual key |
| HTTP API `:3001` | exposes `/bench/run`, `/bench/history`, `/eval/<set>/results`, `/proto/<id>` |

Quirm does NOT have iMessage access. Internal-facing only.

## Postgres

| Database | Access | Purpose |
|---|---|---|
| `hermes_quirm` (owner: `quirm`) | RW | session DB, eval history, prototype registry |
| `ironclaw_frick`, `openclaw_frack`, `hermes_sancho`, `openfang_vetinari`, `ironclaw_vimes`, `openclaw_puck` | RO via `quirm_ro` role | sibling introspection for benchmarks |

## Kubernetes access

ServiceAccount `quirm-ops` in `agents-shared`. Cluster-wide
**read-only** via ClusterRole `quirm-cluster-readonly`. Quirm
introspects pods, deployments, metrics — never modifies them.

## Eval harness layout (in `quirm-graph`)

```
quirm-graph/
├── pages/
│   ├── eval-sets/                       ← test prompt corpora
│   │   ├── invoice-triage-frack.md
│   │   ├── calendar-coordination-sancho.md
│   │   ├── cluster-ops-frick.md
│   │   ├── coordination-vetinari.md
│   │   ├── security-audit-vimes.md
│   │   └── creative-quality-puck.md
│   ├── runs/                            ← per-run results, dated
│   ├── memos/                           ← weekly digests, decision docs
│   ├── prototypes/                      ← in-flight experiments
│   ├── locked-tower/                    ← Vimes-flagged DO NOT DEPLOY
│   └── half-finished/                   ← side puzzles to revisit
└── journals/                            ← daily run logs
```

## Skills (planned, in `quirm-graph/pages/skills/`)

- `bench-run.py` — kicks a benchmark suite against a sibling
- `eval-grade.py` — applies a rubric to model outputs
- `cost-trace.py` — pulls LiteLLM virtual-key spend by tag
- `latency-histogram.py` — render P50/P95/P99 over time
- `regression-detect.py` — alerts when a metric crosses threshold
- `prototype-scaffold.py` — bootstraps a new experiment from template

## Hermes capabilities

Configured in `homelab/config/hermes.toml`. Quirm's enabled toolsets:

- `terminal` (Docker-sandboxed per upstream Hermes)
- `code-execution` (Python via embedded interpreter)
- `web-search` + `browser` (read-only research)
- `memory` (own session DB + cross-graph search)
- `mcp` (in-cluster MCP servers)

Quirm does NOT have `kubectl` write tooling, BlueBubbles, Home
Assistant, or Stripe access. Read everything; mutate nothing
outside its own state.

## Required env vars

Provided by `quirm-secrets` SealedSecret in `agents-shared`:

| Var | Use |
|---|---|
| `LITELLM_API_KEY` | virtual key tagged `agent:quirm` for cost attribution |
| `DATABASE_URL` | `postgres://quirm@homelab-pg-rw...` |
| `QUIRM_RO_PASSWORD` | psql for sibling DB introspection |
| `MATRIX_HOMESERVER` + `MATRIX_ACCESS_TOKEN` | `@quirm:leopaska.xyz` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID_QUIRM` | shared bot, dedicated chat |
| `NTFY_TOKEN` | regression alerts |
| `OFP_SHARED_SECRET` | A2A mutual auth |
| `BW_CLIENTID` + `BW_CLIENTSECRET` | Vaultwarden API-key login (`bw login --apikey`) for ad-hoc credential lookups |
| `HEALTHCHECKS_UUID` | per-agent UUID for hc-ping.com heartbeats |
