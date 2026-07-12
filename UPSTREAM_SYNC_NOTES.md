# Upstream sync — manual resolution required

Generated: 2026-07-12T08:00:29Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 79c08064568665251dac93b79b2247082b0510ee
Behind by:  2225 commits

The automated 3-way merge on top of `origin/main` produced conflicts.
The merge was aborted before any conflict markers were committed, so
this branch currently contains only this notes file on top of
`origin/main` — that is by design.

## Conflicting paths

```
gateway/config.py
gateway/platforms/bluebubbles.py
uv.lock
```

## How to resolve

```bash
git fetch origin "chore/upstream-sync-2026-07-12-79c0806" && git switch "chore/upstream-sync-2026-07-12-79c0806"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-12-79c0806"
```

Then update the PR body / drop draft state and merge.
