# Upstream sync — manual resolution required

Generated: 2026-05-15T03:09:37Z
Upstream:   https://github.com/NousResearch/hermes-agent.git @ main
Upstream commit: 4695d2716f60da89152bdc9dfa7d96e54ea7c22e
Behind by:  208 commits

The automated 3-way merge on top of `origin/main` produced conflicts.
The merge was aborted before any conflict markers were committed, so
this branch currently contains only this notes file on top of
`origin/main` — that is by design.

## Conflicting paths

```
agent/lsp/manager.py
cli.py
gateway/platforms/base.py
gateway/platforms/feishu.py
gateway/platforms/matrix.py
gateway/platforms/slack.py
hermes_cli/main.py
scripts/release.py
tests/agent/lsp/test_service.py
tests/tools/test_clarify_gateway.py
tools/file_operations.py
tools/web_tools.py
website/docs/reference/cli-commands.md
website/docs/user-guide/features/lsp.md
```

## How to resolve

```bash
git fetch origin "chore/upstream-sync-2026-05-15-4695d27" && git switch "chore/upstream-sync-2026-05-15-4695d27"
git remote add upstream https://github.com/NousResearch/hermes-agent.git 2>/dev/null || true
git fetch upstream main
git merge upstream/main
# resolve, then:
git rm UPSTREAM_SYNC_NOTES.md
git commit
git push --force origin "chore/upstream-sync-2026-05-15-4695d27"
```

Then update the PR body / drop draft state and merge.
