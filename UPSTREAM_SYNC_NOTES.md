# Upstream sync — manual resolution required

Generated: 2026-07-11T08:10:28Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 3b2ef789dfcf92f5b7b18c08c59d25948e50857f
Behind by:  2180 commits

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
git fetch origin "chore/upstream-sync-2026-07-11-3b2ef78" && git switch "chore/upstream-sync-2026-07-11-3b2ef78"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-11-3b2ef78"
```

Then update the PR body / drop draft state and merge.
