# Local patches vs upstream NousResearch/hermes-agent

Track non-additive changes (anything outside `homelab/`).

## Active patches

### bluebubbles-proxy-gate: enable BlueBubbles via proxy env pair

- **File**: `gateway/config.py`
- **Reason**: the homelab routes iMessage through the in-cluster
  `bluebubbles-proxy` (BLUEBUBBLES_PROXY_URL + BLUEBUBBLES_API_KEY)
  instead of a direct BB-server URL/password. Upstream only enables the
  platform when `BLUEBUBBLES_SERVER_URL and BLUEBUBBLES_PASSWORD` are
  set; we widen the gate to also accept the proxy pair.
- **Upstream PR**: not submitted (homelab-specific substrate).
- **Last applied**: 2026-07-26 against upstream@fe431651c (clean merge,
  no reapply needed).

### bluebubbles-guid-fallback: `any;-;<handle>` outbound chat creation

- **File**: `gateway/platforms/bluebubbles.py` (`_resolve_chat_guid`)
- **Reason**: upstream returns `None` when no exact chat identity
  matches, which breaks agent-initiated first-contact iMessages. We keep
  the t3 fix (2026-05-12): synthesize `any;-;<handle>` for phone/email
  handles (BB-server creates the 1:1 chat if missing) and resolve
  symbolic slug targets to the configured home DM. Upstream's strict
  chatIdentifier-only matching (no participant fallback, #24157) is
  retained.
- **Upstream PR**: not submitted.
- **Last applied**: 2026-07-26 against upstream@fe431651c (clean merge,
  no reapply needed).

Quirm's own work — eval harness, benchmark scripts — lives in
`quirm-graph/pages/`, not in source code.
