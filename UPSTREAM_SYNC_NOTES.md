# Upstream sync — manual resolution required

Generated: 2026-07-13T08:13:24Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: e4ea0a0ed7fc24761b2b425146893561a73216e1
Behind by:  2304 commits

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
git fetch origin "chore/upstream-sync-2026-07-13-e4ea0a0" && git switch "chore/upstream-sync-2026-07-13-e4ea0a0"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-13-e4ea0a0"
```

Then update the PR body / drop draft state and merge.
