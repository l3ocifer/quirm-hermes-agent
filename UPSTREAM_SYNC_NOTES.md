# Upstream sync — manual resolution required

Generated: 2026-07-12T08:10:12Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: bdfc7c0b1e65d157646411d858a4632d912699fd
Behind by:  2231 commits

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
git fetch origin "chore/upstream-sync-2026-07-12-bdfc7c0" && git switch "chore/upstream-sync-2026-07-12-bdfc7c0"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-12-bdfc7c0"
```

Then update the PR body / drop draft state and merge.
