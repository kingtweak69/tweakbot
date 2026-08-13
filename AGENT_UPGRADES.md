# TweakBot Agent Runtime Upgrades

This build adds the agent upgrades in priority order without replacing the existing GitHub OAuth, Railway OAuth, AI provider transport, Wavelink music/DJ, or ElevenLabs command stack.

## 1. Capability registry
`bot.capabilities` is the shared registry for AI-callable actions. Cogs register and unregister their own capabilities, and `personality.py` consumes the registry rather than maintaining a hard-coded list.

## 2. Persistent agent jobs
PostgreSQL-backed jobs survive restarts and keep full step/tool transcripts. The model-facing transcript is automatically compacted on long jobs while `AGENT_JOB_MAX_STEPS=0` remains unlimited. Commands: `job start`, `job list`, `job status`, `job cancel`, `job resume`.

## 3. Persistent memory
Conversation context, rolling summaries, durable user/guild memories, and tool events are stored separately in PostgreSQL. Ungrounded capability refusals are not retained as conversation context.

## 4. Repository intelligence
GitHub-backed repository inspection includes metadata, trees, file reads, code search, comparisons, and stack/deployment detection using the requester's existing GitHub OAuth context.

## 5. Guarded code workspace
Persistent source workspaces support safe reads/search/edits/diffs and known compile/test/build checks. The agent is not given an arbitrary shell capability. Child checks run with a scrubbed environment and resource/time limits.

## 6. GitHub + Railway orchestration
The agent can inspect Railway status/logs, inspect variable names without exposing values, commit a tested workspace to GitHub, deploy an exact commit SHA, and diagnose a GitHub/Railway deployment path.

## 7. Multimodal attachments
Images/screenshots, PDFs, ZIP/source files, audio/voice, and representative video frames are converted into context for the normal chat/agent loop. Audio uses the configured OpenAI-compatible transcription endpoint.

## 8. Live voice agent
`voiceagent start` creates an explicit single-speaker voice session for the requester. Speech is transcribed, passed through the normal agent, and spoken with ElevenLabs. Raw voice audio is not persisted. Live voice-agent mode does not replace an active Wavelink music connection; disconnect music first.

## 9. Voice Studio
Per-user ElevenLabs voice selection plus instrumental mixing. `voicestudio` and the `voice_studio_mix` capability support speak/rap/sing generation, vocal delay, levels, ducking, looping, normalization, fades, and MP3/WAV output through FFmpeg.

## 10. Self-diagnostics
`agenthealth [deep]` / `diagnostics` / `diag` and the `system_health` capability report agent registry, PostgreSQL, AI endpoint, GitHub/Railway linkage, Lavalink, ElevenLabs, FFmpeg, workspace/job state, and cog health without printing credentials. The existing owner `health` command also includes the agent health report when available.

## New runtime dependencies
- `discord-ext-voice-recv==0.5.2a179`
- `pypdf>=5.0.0`

Railway installs these through `requirements.txt` on deploy. FFmpeg was already part of the existing Nixpacks setup.


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
