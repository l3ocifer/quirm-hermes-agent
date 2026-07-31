# Upstream sync — manual resolution required

Generated: 2026-07-31T08:00:05Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: f3cda0ceb18d8ba7465a6d223098ef0e56c8fee1
Behind by:  1668 commits

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
git fetch origin "chore/upstream-sync-2026-07-31-f3cda0c" && git switch "chore/upstream-sync-2026-07-31-f3cda0c"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-31-f3cda0c"
```

Then update the PR body / drop draft state and merge.
