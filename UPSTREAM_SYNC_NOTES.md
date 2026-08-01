# Upstream sync — manual resolution required

Generated: 2026-08-01T08:00:04Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 40e0e7ad56f7faac24c757b11d3ef6f0f9b83de4
Behind by:  1954 commits

The automated 3-way merge on top of `origin/main` produced conflicts.
The merge was aborted before any conflict markers were committed, so
this branch currently contains only this notes file on top of
`origin/main` — that is by design.

## Conflicting paths

```
tests/gateway/test_bluebubbles.py
```

## How to resolve

```bash
git fetch origin "chore/upstream-sync-2026-08-01-40e0e7a" && git switch "chore/upstream-sync-2026-08-01-40e0e7a"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-08-01-40e0e7a"
```

Then update the PR body / drop draft state and merge.
