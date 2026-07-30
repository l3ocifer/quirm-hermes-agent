# Upstream sync — manual resolution required

Generated: 2026-07-30T08:00:05Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: b5ca90050886b171da2802cf27b80955c3d18cb5
Behind by:  1546 commits

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
git fetch origin "chore/upstream-sync-2026-07-30-b5ca900" && git switch "chore/upstream-sync-2026-07-30-b5ca900"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-07-30-b5ca900"
```

Then update the PR body / drop draft state and merge.
