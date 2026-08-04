# Upstream sync — manual resolution required

Generated: 2026-08-04T08:02:16Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 4075c8fd5ade673c93f757903b003ddc52603577
Behind by:  2663 commits

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
git fetch origin "chore/upstream-sync-2026-08-04-4075c8f" && git switch "chore/upstream-sync-2026-08-04-4075c8f"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-08-04-4075c8f"
```

Then update the PR body / drop draft state and merge.
