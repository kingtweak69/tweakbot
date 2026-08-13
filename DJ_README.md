# TweakBot AI DJ patch

This patch is based on the August 10 `tweakbot(2).zip` Wavelink build.

## What changed
- `cogs/music.py`: per-guild AI DJ state, autonomous style-aware queue seeding, Wavelink AutoPlay recommendations, energy/style controls, requests, and natural-language tool methods.
- `cogs/personality.py`: exposes the music/DJ functions to the existing OpenAI-compatible tool-calling loop.
- `utils/ai.py`: unchanged; it already transports `tools` payloads and is not the command executor in this build.

## Natural-language examples
- `Tweak, DJ for us — Atlanta trap, energy 9.`
- `Go darker after this.`
- `Bring the energy down to 4.`
- `Play Faneto next.`
- `Skip this.`
- `Pause it.`
- `Turn it down to 60.`
- `Stop DJing.`

## Command fallbacks
- `/dj [style]`
- `/djoff`
- `/djstyle <style>`
- `/djenergy <1-10>`
- `/djrequest <song>`
- `/djstatus`

`AI_MUSIC_TOOLS_ENABLED` defaults to true. Set it false if you ever want conversational music actions disabled while keeping normal commands.
