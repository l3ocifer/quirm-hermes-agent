# Upstream sync — manual resolution required

Generated: 2026-10-09T08:04:20Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main (fetched from https://git.leopaska.xyz/leo/hermes-agent.git)
Upstream commit: 596eacadc95127e2e07318887d10bf9ea7c87a66
Behind by:  32924 commits

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
git fetch origin "chore/upstream-sync-2026-10-09-596eaca" && git switch "chore/upstream-sync-2026-10-09-596eaca"
git remote add upstream https://git.leopaska.xyz/leo/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-10-09-596eaca"
```

Then update the PR body / drop draft state and merge.
