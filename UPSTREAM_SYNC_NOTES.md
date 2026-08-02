# Upstream sync — manual resolution required

Generated: 2026-08-02T08:00:05Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 840fb55a8aaeb69bfcd6f34a80e57f9a5bcd44ce
Behind by:  2133 commits

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
git fetch origin "chore/upstream-sync-2026-08-02-840fb55" && git switch "chore/upstream-sync-2026-08-02-840fb55"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-08-02-840fb55"
```

Then update the PR body / drop draft state and merge.
