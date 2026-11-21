# Upstream sync — manual resolution required

Generated: 2026-11-21T09:00:16Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main (fetched from https://git.leopaska.xyz/leo/hermes-agent.git)
Upstream commit: aeb5a071069dbd85d85f2c8d44337f16df29d475
Behind by:  57409 commits

The automated 3-way merge on top of `origin/main` produced conflicts.
The merge was aborted before any conflict markers were committed, so
this branch currently contains only this notes file on top of
`origin/main` — that is by design.

## Conflicting paths

```
.dockerignore
.github/CODEOWNERS
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
git fetch origin "chore/upstream-sync-2026-11-21-aeb5a07" && git switch "chore/upstream-sync-2026-11-21-aeb5a07"
git remote add upstream https://git.leopaska.xyz/leo/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-11-21-aeb5a07"
```

Then update the PR body / drop draft state and merge.
