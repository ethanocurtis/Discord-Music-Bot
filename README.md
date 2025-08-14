# utila-music-bot

Production-ready Discord music bot in Python, streaming YouTube (and basic SoundCloud/HTTP links) via **yt-dlp → FFmpeg → Discord voice** (no Lavalink). Packaged for Docker.

## Features

- Slash commands: `/play /pause /resume /skip /stop /queue /np /volume /clear /shuffle /remove /move /seek /loop`  
- Reaction controls on the "Now Playing" embed: ⏯ ⏭ ⏹ 🔁 🔀 🔉 🔊 ⏮
- Per-guild players with independent queues, loop (off/track/queue), shuffle, seek, volume (0–200%).
- Safe concurrency with per-guild locks; cancellation-safe skipping/stopping.
- Auto-disconnect after idle timeout; auto-reconnect on moves.
- Debounced embed updates (≤ 1/sec) with textual progress bar.
- Structured logs to stdout.
- Dockerized with pinned deps and healthcheck.

## Prerequisites

1. **Discord Application & Bot**
   - Create at <https://discord.com/developers/applications>
   - Add a Bot, copy **Token**.
   - Required Privileged Intents: *none* (this bot uses minimal intents).
2. **Permissions when inviting**
   - Send Messages
   - Embed Links
   - Add Reactions
   - Read Message History
   - Manage Messages (to remove user reactions on the panel; optional)
   - Connect
   - Speak
3. **System**
   - Linux host with Docker & Docker Compose
   - `ffmpeg` is installed in the image

## Install

```bash
git clone https://github.com/yourname/utila-music-bot.git
cd utila-music-bot
cp .env.example .env
# Edit .env and paste your DISCORD_TOKEN
docker compose up --build -d
```

### Command Registration

- **Faster (dev):** set `DEV_GUILDS=...` with your test server ID(s). Commands appear instantly.
- **Global:** leave `DEV_GUILDS` blank. Global sync can take up to ~1 hour.

## Usage

1. Invite the bot with the permissions above.  
2. In a text channel, run `/play <url or search terms>` while **you are in a voice channel**.  
3. The bot joins your channel, queues a track, and posts a **Now Playing** embed with reaction controls.  
4. Use additional commands to manage the queue: `/queue`, `/remove`, `/move`, `/shuffle`, `/seek`, `/loop`, `/volume`.

### Notes on Audio/Bitrate/Latency

- Discord voice caps bitrate server-side; this bot streams the best available audio then encodes Opus via FFmpeg.
- Volume changes restart the FFmpeg pipeline to apply a `volume` filter; brief hiccup is expected.
- Latency depends on your host network and FFmpeg buffers (kept conservative).

## Configuration

See `.env.example`:

- `DISCORD_TOKEN` (**required**)
- `DEV_GUILDS` (optional, comma-separated)
- `OWNER_IDS` (optional; bypass channel checks)
- `LOG_LEVEL` (`INFO` default)
- `IDLE_DISCONNECT_MINUTES` (`5` default)

## Troubleshooting

**No audio / bot is connected but silent**
- Ensure the host CPU supports FFmpeg Opus encoding (it does in Debian slim image).
- Check container logs: `docker compose logs -f utila-music-bot`.
- Some regional YouTube links geo-block direct audio; try another track.

**"Unknown encoder 'libopus'"**
- The image uses Debian FFmpeg with Opus support. If you customized the Dockerfile, ensure `ffmpeg -codecs | grep opus` shows encoders.

**Commands not showing**
- If using global commands, allow time to propagate.
- For instant dev: set `DEV_GUILDS` to your test guild ID and rebuild/restart.

**Reactions don't work**
- The bot needs `Read Message History`, `Add Reactions`, and ideally `Manage Messages` to clean up reactions.
- You must be in the **same voice channel as the bot** to control playback via reactions.

**Search fails or YouTube throttles**
- Try a direct URL. `yt-dlp` has built-in retries with timeouts; transient errors are handled gracefully.

**Bot doesn't leave after songs**
- It waits for idle (no queue and nothing playing) for `IDLE_DISCONNECT_MINUTES` before disconnecting.

## Development

- Code uses type hints and is organized in a single `bot.py` with centralized FFmpeg options under `audio/ffmpeg_opts.py`.
- Recommended linters: black/ruff (not included in requirements).

## License

MIT — see `LICENSE`.

---

## Quick Start (copy/paste)

```bash
cp .env.example .env
# edit .env and put your token
docker compose up --build -d
```

### Invite URL scopes/permissions

- Scopes: `bot applications.commands`  
- Permissions to include: Send Messages, Embed Links, Add Reactions, Read Message History, Manage Messages, Connect, Speak

Example (replace `CLIENT_ID`):
```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&permissions=277103747072&scope=bot%20applications.commands
```
*(The numeric permissions integer corresponds to the permissions listed above.)*

---
