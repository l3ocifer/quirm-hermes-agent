# Changelog

Quirm-Hermes-Agent releases.

## Unreleased

### Added

- Initial homelab/ overlay scaffolding (mirrors sancho-hermes-agent
  layout — same upstream framework, different persona)
- Dockerfile installs Hermes from local fork via `uv pip install .`
- k8s manifests for `agents-shared` namespace + floating
  (soft-prefer thebeast for RAM-heavy benchmarking workloads) +
  Longhorn-backed (longhorn-single state, longhorn-rwx graphs)
- config/SOUL.md (Leonardo-of-Quirm persona)
- config/TOOLS.md (eval harness layout, RO Postgres + cluster access)
- config/hermes.toml (LiteLLM, A2A peers for all 6 siblings, cron tasks)
- GitHub Actions: build.yml, upstream-sync.yml (Sun 03:00 UTC),
  shared-docs-bump.yml
- Submodule of l3ocifer/homelab at homelab/shared/

### New repo

- Created fresh as `l3ocifer/quirm-hermes-agent` (cannot be a GitHub
  fork because `l3ocifer/sancho-hermes-agent` already forks the same
  upstream; instead, we cloned upstream and rewired remotes — see
  README.md "Sync from upstream" for the workflow).
