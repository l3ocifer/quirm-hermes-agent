# Upstream sync — manual resolution required

Generated: 2026-11-02T09:00:15Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main (fetched from https://git.leopaska.xyz/leo/hermes-agent.git)
Upstream commit: e534ed0b08cce24195be4cc20a73dc47aa2192a0
Behind by:  43124 commits

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
git fetch origin "chore/upstream-sync-2026-11-02-e534ed0" && git switch "chore/upstream-sync-2026-11-02-e534ed0"
git remote add upstream https://git.leopaska.xyz/leo/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-11-02-e534ed0"
```

Then update the PR body / drop draft state and merge.
