# Upstream sync — manual resolution required

Generated: 2026-09-15T08:03:29Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: a55c972e09177e4db3934915e329993858b247d6
Behind by:  16974 commits

The automated 3-way merge on top of `origin/main` produced conflicts.
The merge was aborted before any conflict markers were committed, so
this branch currently contains only this notes file on top of
`origin/main` — that is by design.

## Conflicting paths

```
.dockerignore
AGENTS.md
gateway/config.py
gateway/platforms/bluebubbles.py
hermes_cli/main.py
plugins/platforms/teams/adapter.py
plugins/platforms/telegram/adapter.py
tests/gateway/test_bluebubbles.py
tools/file_operations.py
tools/send_message_tool.py
```

## How to resolve

```bash
git fetch origin "chore/upstream-sync-2026-09-15-a55c972" && git switch "chore/upstream-sync-2026-09-15-a55c972"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-09-15-a55c972"
```

Then update the PR body / drop draft state and merge.
