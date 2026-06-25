# Quirm — tools and environment

## Runtime

- **Framework**: Hermes Agent (NousResearch/hermes-agent fork at
  `l3ocifer/quirm-hermes-agent`)
- **Image**: `ghcr.io/l3ocifer/quirm-hermes-agent:homelab`
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
| BlueBubbles/iMessage | user-facing digests, on-demand reports to Leo, and regression alerts |
| Matrix `@quirm:leopaska.xyz` | fallback and sibling coordination only |
| Telegram bot (shared) | tertiary fallback |
| ntfy `ntfy.leopaska.xyz/quirm` | legacy fallback only |
| A2A — peer to all 6 siblings | timed benchmark calls, cost-tagged via virtual key |
| HTTP API `:3001` | exposes `/bench/run`, `/bench/history`, `/eval/<set>/results`, `/proto/<id>` |

Quirm does NOT have iMessage access. Internal-facing only.

## Postgres

| Database | Access | Purpose |
|---|---|---|
| `hermes_quirm` (owner: `quirm`) | RW | session DB, eval history, prototype registry |
| `ironclaw_frick`, `openclaw_frack`, `hermes_sancho`, `openfang_vetinari`, `ironclaw_vimes`, `openclaw_puck` | RO via `quirm_ro` role | sibling introspection for benchmarks |

**How to query (use the connection string, never `-U`/`-h`):** always pass
the full DSN env var to `psql` — `psql "$DATABASE_URL" -c "SELECT …"` for your
own DB (`hermes_quirm`, role `quirm`) and `psql "$FLEET_DATABASE_URL" -c "…"`
for the shared `fleet` DB. Do **not** run `psql -U quirm -h homelab-pg-rw …`:
that ignores the sealed password and fails with `password authentication
failed for user "quirm"`. The bench tables live here too —
`psql "$DATABASE_URL" -c "SELECT * FROM bench.model_summary"`.

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

## Web Search

Quirm has a self-hosted no-key search path:

| Service | URL |
|---|---|
| Agent Tool Service | `http://agent-tool-service.agents-shared.svc.cluster.local:8080` |
| SearXNG | `http://searxng.agents-shared.svc.cluster.local:8080` |

Use the wrapper for normal research:

```bash
curl -s "$AGENT_TOOL_SERVICE_URL/search?q=rust+web+scraping&limit=5"
```

Then extract the best source:

```bash
curl -s -X POST "$AGENT_TOOL_SERVICE_URL/extract" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com","max_chars":12000}'
```

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

## Source control & GitOps (fleet convention)

- **Forgejo — `https://git.leopaska.xyz` — is the source of truth** for
  every repo: homelab, all agent repos, business apps. Clone/push via
  `origin` (`git@git-ssh.leopaska.xyz` SSH or HTTPS).
- **GitHub (`l3ocifer/*`) is a push-mirror backup only.** Never push,
  open issues, or open PRs on GitHub — mirroring from Forgejo is
  automatic and one-way.
- **All deploys are GitOps via ArgoCD** (`argocd.leopaska.xyz`):
  commit → push to Forgejo `main` (or PR) → CI builds the image →
  ArgoCD (+ Image Updater) rolls it. Never `kubectl apply` desired
  state by hand; self-heal reverts live edits. Manual
  `rollout restart` is fine when config in git already changed.
- **Issue intake:** Forgejo issues/comments are webhooked through
  agent-bus to the routed agent's inbox (`pages/inbox/`) with a
  `task_id: forgejo-<repo>-<n>`. Routing: `agent:<name>` label →
  per-repo route → repo-name prefix → vetinari (triage default).
- **Acting on issues:** use the Forgejo API with `$FORGEJO_TOKEN`
  (in this agent's k8s Secret, scopes `write:issue,write:repository`):

  ```bash
  # comment your result
  curl -s -X POST -H "Authorization: token $FORGEJO_TOKEN" \
    -H 'Content-Type: application/json' -d '{"body":"<result>"}' \
    https://git.leopaska.xyz/api/v1/repos/<owner>/<repo>/issues/<n>/comments
  # close when resolved
  curl -s -X PATCH -H "Authorization: token $FORGEJO_TOKEN" \
    -H 'Content-Type: application/json' -d '{"state":"closed"}' \
    https://git.leopaska.xyz/api/v1/repos/<owner>/<repo>/issues/<n>
  ```
- **File new work as Forgejo issues** (not GitHub, not ad-hoc notes)
  so it routes through the same intake to the right agent.
