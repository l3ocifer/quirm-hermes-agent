# Quirm — Hermes-Agent researcher / benchmarker

This is **Leo's fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)**,
extended to run as `Quirm` — the seventh agent in [Leo's homelab](https://github.com/l3ocifer/homelab),
named for **Leonardo of Quirm** (the eccentric Discworld inventor).

Quirm's province: benchmarking the rest of the fleet, evaluating new
tools/models, and prototyping new capabilities. Read everything; mutate
nothing outside its own state. PRs only — Frick deploys.

## Layout

```
quirm-hermes-agent/                  ← repo root (this fork)
├── (upstream hermes-agent source)
│   ├── agent/
│   ├── acp_adapter/
│   ├── acp_registry/
│   ├── pyproject.toml
│   └── ...
└── homelab/                          ← everything we add
    ├── Dockerfile                    ← Python 3.13 + pip install . from local
    ├── k8s/                          ← kustomize tree
    ├── config/                       ← SOUL.md, TOOLS.md, hermes.toml
    ├── shared/                       ← submodule → l3ocifer/homelab
    ├── .github/workflows/
    ├── PATCHES.md, CHANGELOG.md, README.md
```

## Persona, in 30 seconds

Soft-spoken, courtly, constantly tangential. Show him a problem, get
four prototypes back — two ridiculous, one dangerous, one better than
anything else in the room. Vetinari directs him; Vimes audits him.
Without that scaffolding he produces seven half-finished prototypes
overnight and forgets which is which. With it, he is the most
productive force in the fleet.

See `config/SOUL.md` for the full persona.

## Required env vars

Provided by `quirm-secrets` SealedSecret in `agents-shared` namespace.
See `config/hermes.toml` for the full reference.

## Build locally

```bash
git clone --recursive https://github.com/l3ocifer/quirm-hermes-agent
cd quirm-hermes-agent
docker build -f homelab/Dockerfile \
  -t ghcr.io/l3ocifer/quirm-hermes-agent:dev .
```

## Sync from upstream

```bash
git fetch upstream
git merge upstream/main          # or use the weekly auto-PR from CI
```

## License

- Hermes-Agent upstream: MIT (see `../LICENSE`).
- Homelab additions in `homelab/`: same.
- Persona text in `config/SOUL.md` is Leo Paska's IP.
