# Interactive account linking setup

## Credential encryption

Generate a Fernet key once and keep it private:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```


## GitHub

1. Create a GitHub OAuth App.
2. Enable **Device Flow** in the app settings.
3. Set `GITHUB_OAUTH_CLIENT_ID` to the app client ID.
4. Users run `Tb$gh login`, open the device URL, and enter the code.

No GitHub client secret is required for device flow.

## Railway

1. Create a Railway OAuth app in the workspace Developer settings.
2. Register this exact callback URL:

   `https://YOUR_PUBLIC_HOST/oauth/railway/callback`

3. Set:

```env
RAILWAY_OAUTH_CLIENT_ID=
RAILWAY_OAUTH_CLIENT_SECRET=
OAUTH_PUBLIC_BASE_URL=https://YOUR_PUBLIC_HOST
OAUTH_CALLBACK_HOST=127.0.0.1
OAUTH_CALLBACK_PORT=8787
```

4. Reverse-proxy the public HTTPS callback path to the callback listener. Example Caddy route:

```caddy
bot.example.com {
    reverse_proxy /oauth/railway/callback 127.0.0.1:8787
}
```

Users run `Tb$railway login` and authorize their own Railway account.


## Credential retention

OAuth tokens are session-only and are never persisted. Users authenticate on the provider's own site, where GitHub/Railway passkeys, passwords, MFA, and security keys can be used. TweakBot receives only the OAuth authorization result and keeps the resulting token in RAM until restart, expiry, or logout.


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
