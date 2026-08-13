# Ephemeral OAuth + Passkey/Provider Sign-In

TweakBot's GitHub and Railway integrations use **ephemeral OAuth sessions**.

## What the user does

1. Run `gh login` or `railway login`.
2. TweakBot opens/prints the provider's normal authorization page.
3. The user authenticates **on GitHub/Railway** using whatever that provider supports, including a passkey, password, MFA, or security key.
4. The provider redirects/returns an OAuth authorization result.
5. TweakBot keeps the resulting access/refresh token only in process memory.

TweakBot never receives or stores the user's GitHub/Railway password or passkey private key.

## What is persisted

Nothing from the OAuth session is persisted by TweakBot:
- no access token
- no refresh token
- no password
- no passkey private key
- no OAuth account metadata

Legacy `linked_accounts` rows are purged at database startup.

## Tradeoff

A restart, redeploy, crash, or process replacement destroys all linked OAuth sessions. Users must authenticate again.

A provider's own passkey is **not** an OAuth replacement. A passkey authenticates the user to GitHub/Railway; OAuth is what grants TweakBot temporary API access.

The configured `GITHUB_TOKEN` and Railway OAuth client secret remain deployment configuration values. They are not per-user linked credentials.


## Persistence boundary

TweakBot intentionally has two separate persistence domains:

- **Durable:** conversations, summaries, user/guild memories, server settings, moderation records, media accounting, agent jobs, job state/steps, and audit events are stored in PostgreSQL and survive restarts, crashes, and redeploys when `DATABASE_URL` points at persistent Postgres.
- **Ephemeral:** GitHub/Railway OAuth access tokens and refresh tokens exist only in the running process. They are never written to PostgreSQL, files, backups, logs, agent memory, or job state. A restart/redeploy/crash requires the user to authenticate again.

The container filesystem is not treated as durable storage. Agent job state and memory are deliberately kept in PostgreSQL.


## Persistence boundary

TweakBot persists agent jobs, memory, conversation state, configuration, audit events,
and coding workspace metadata in PostgreSQL. Coding workspace files live under
`AGENT_WORKSPACE_ROOT`; on Railway this directory should be mounted on a persistent
Volume (for example `/data`). OAuth access/refresh tokens are deliberately excluded
from PostgreSQL, backups, audit records, and workspace files. They exist only in
process memory and disappear on restart/redeploy/crash.

## OAuth logout

Logout always destroys the local RAM session. GitHub sessions are additionally sent
to GitHub's credential-revocation endpoint when possible. Railway local-session
logout is still immediate; provider-side revocation is only attempted when an
explicit `RAILWAY_OAUTH_REVOKE_URL` is configured by the operator, because TweakBot
does not assume an undocumented Railway revocation endpoint.
