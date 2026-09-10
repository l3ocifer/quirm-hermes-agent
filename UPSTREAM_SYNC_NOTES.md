# Upstream sync — manual resolution required

Generated: 2026-09-10T08:05:08Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 8068c09432d3e7a23d30aa5f4f7d5a17783f8668
Behind by:  15339 commits

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
git fetch origin "chore/upstream-sync-2026-09-10-8068c09" && git switch "chore/upstream-sync-2026-09-10-8068c09"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-09-10-8068c09"
```

Then update the PR body / drop draft state and merge.
