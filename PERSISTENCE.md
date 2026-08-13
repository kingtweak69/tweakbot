# TweakBot persistence boundary

## Durable state

PostgreSQL persists bot state, conversations, summaries, AI memories, agent jobs,
job steps, tool audit events, guild configuration, moderation data, media accounting,
and coding-workspace metadata.

## Coding workspaces

Workspace files are stored under `AGENT_WORKSPACE_ROOT`.

For Railway, attach a persistent Volume and mount it at `/data`, then use:

```env
AGENT_WORKSPACE_ROOT=/data/tweakbot-workspaces
AGENT_WORKSPACE_PERSISTENT=true
```

This makes workspaces survive process crashes and Railway redeploys. Without a
durable volume, PostgreSQL metadata survives but the actual workspace files do not.

## OAuth exception

GitHub and Railway access/refresh tokens are process-local only. They are not
written to PostgreSQL, backups, audit records, or workspace metadata. They are
destroyed on process exit/restart/redeploy and expire from the in-memory vault
after 24 hours.

GitHub logout also submits the token to GitHub's credential revocation endpoint.
Railway logout only performs provider-side revocation when the operator explicitly
configures `RAILWAY_OAUTH_REVOKE_URL`; no undocumented endpoint is assumed.

## AI destructive operations

AI destructive capabilities are blocked by default at the capability registry,
independently of model instructions. Enable `AI_DESTRUCTIVE_TOOLS_ENABLED` only
when you intentionally want the model to execute destructive operations.
