from __future__ import annotations
import os, asyncio, logging, typing as T
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

if not discord.opus.is_loaded():
    # On Debian/Ubuntu, the shared object is libopus.so.0
    discord.opus.load_opus("libopus.so.0")
# ---------- Dependencies (must be installed in your image/env) ----------
try:
    import yt_dlp  # stream extraction
except Exception as e:
    raise SystemExit("yt-dlp is required. Install it and rebuild. Error: %r" % (e,))

try:
    import nacl  # noqa: F401  # voice support (PyNaCl)
except Exception as e:
    raise SystemExit("PyNaCl is required for voice. Install 'PyNaCl'. Error: %r" % (e,))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music-bot")

# ---------- Config via env ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
YTDLP_COOKIEFILE = os.getenv("YT_COOKIE_FILE")  # Optional cookiefile for age/region-locked content
IDLE_DISCONNECT_SECONDS = int(os.getenv("IDLE_DISCONNECT_SECONDS", "300"))  # 5 minutes
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "100"))
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "80"))  # percent

# ---------- yt-dlp ----------
YTDLP_OPTS_BASE: dict = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "default_search": "ytsearch",
    "cachedir": False,
    "retries": 3,
    "timeout": 15,
}
if YTDLP_COOKIEFILE:
    YTDLP_OPTS_BASE["cookiefile"] = YTDLP_COOKIEFILE
YTDLP_OPTS_SEARCH = {**YTDLP_OPTS_BASE, "noplaylist": True}

# ---------- FFmpeg ----------
FFMPEG_BEFORE = [
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
    "-nostdin",
]
FFMPEG_OPTIONS = "-vn"

LoopMode = T.Literal["off", "one", "all"]

# ---------- Data classes ----------
@dataclass
class Track:
    title: str
    url: str                # webpage URL for embeds
    stream_url: str         # direct stream URL for ffmpeg
    duration: T.Optional[int] = None
    uploader: T.Optional[str] = None
    requester_id: int = 0
    source: str = "YouTube"
    thumbnail: T.Optional[str] = None

@dataclass
class GuildState:
    guild_id: int
    voice: T.Optional[discord.VoiceClient] = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # of Track
    now_playing: T.Optional[Track] = None
    player_task: T.Optional[asyncio.Task] = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    idle_since: float = 0.0
    volume: int = DEFAULT_VOLUME
    loop: LoopMode = "off"

# ---------- Bot ----------
class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False  # not needed for slash commands
        super().__init__(command_prefix="!", intents=intents)
        # commands.Bot already exposes `self.tree` for slash commands; don't overwrite it.
        self._states: dict[int, GuildState] = {}
        self.activity = discord.Game(name="/play music")
        # Optional: remove legacy prefix help to keep things tidy
        self.remove_command("help")

    # ---- Utilities ----
    def state(self, guild_id: int) -> GuildState:
        st = self._states.get(guild_id)
        if not st:
            st = GuildState(guild_id=guild_id)
            self._states[guild_id] = st
        return st

    @staticmethod
    def looks_like_url(query: str) -> bool:
        return query.startswith(("http://", "https://"))

    async def ensure_connected(self, interaction: discord.Interaction) -> T.Optional[discord.VoiceClient]:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return None
        if interaction.user is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Could not resolve your voice state.", ephemeral=True)
            return None
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You must be connected to a voice channel.", ephemeral=True)
            return None

        channel = interaction.user.voice.channel
        st = self.state(interaction.guild.id)

        if st.voice and st.voice.is_connected():
            if st.voice.channel != channel:
                await st.voice.move_to(channel)
        else:
            st.voice = await channel.connect(self_deaf=True, reconnect=True)
        return st.voice

    async def extract_tracks(self, query: str, requester_id: int) -> list[Track]:
        loop = asyncio.get_event_loop()
        def _extract() -> list[Track]:
            ydl_opts = YTDLP_OPTS_BASE if self.looks_like_url(query) else YTDLP_OPTS_SEARCH
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
            if not info:
                return []
            if "entries" in info:
                entries = [e for e in info["entries"] or [] if e][:MAX_PLAYLIST_ITEMS]
            else:
                entries = [info]

            tracks: list[Track] = []
            for e in entries:
                stream_url = e.get("url") or e.get("webpage_url") or query
                tracks.append(Track(
                    title=e.get("title") or "Unknown title",
                    url=e.get("webpage_url") or e.get("original_url") or query,
                    stream_url=stream_url,
                    duration=e.get("duration"),
                    uploader=e.get("uploader") or e.get("channel"),
                    requester_id=requester_id,
                    source=e.get("extractor_key") or "YouTube",
                    thumbnail=e.get("thumbnail"),
                ))
            return tracks
        return await loop.run_in_executor(None, _extract)

    async def play_track(self, st: GuildState) -> bool:
        if not st.voice or not st.voice.is_connected() or not st.now_playing:
            return False
        try:
            before = " ".join(FFMPEG_BEFORE)
            src = discord.FFmpegPCMAudio(st.now_playing.stream_url, before_options=before, options=FFMPEG_OPTIONS)
            pcm = discord.PCMVolumeTransformer(src, volume=st.volume / 100.0)
            if st.voice.is_playing() or st.voice.is_paused():
                st.voice.stop()
            st.stop_event.clear()
            st.voice.play(pcm, after=lambda e: st.stop_event.set())
            return True
        except Exception:
            log.exception("[%s] FFmpeg play failed", st.guild_id)
            return False

    async def player_loop(self, guild_id: int):
        st = self.state(guild_id)
        log.info("[%s] Player loop started", guild_id)
        try:
            while True:
                # Disconnect after idle period
                if st.queue.empty() and (not st.voice or not st.voice.is_connected() or not st.voice.is_playing()):
                    if st.idle_since == 0:
                        st.idle_since = asyncio.get_event_loop().time()
                    elif asyncio.get_event_loop().time() - st.idle_since > IDLE_DISCONNECT_SECONDS:
                        if st.voice and st.voice.is_connected():
                            await st.voice.disconnect(force=False)
                        st.idle_since = 0
                        await asyncio.sleep(1)
                        continue
                else:
                    st.idle_since = 0

                # If playing/paused, just wait
                if st.voice and (st.voice.is_playing() or st.voice.is_paused()):
                    await asyncio.sleep(1)
                    continue

                # Loop behavior
                if st.now_playing and st.loop == "one":
                    pass  # replay same
                elif st.now_playing and st.loop == "all":
                    await st.queue.put(st.now_playing)
                    st.now_playing = None

                if not st.now_playing:
                    try:
                        st.now_playing = await asyncio.wait_for(st.queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        await asyncio.sleep(1)
                        continue

                started = await self.play_track(st)
                if not started:
                    bad = st.now_playing
                    st.now_playing = None
                    if bad:
                        log.warning("[%s] Dropping track due to playback failure: %s", guild_id, bad.title)
                    await asyncio.sleep(1)
                    continue

                try:
                    await asyncio.wait_for(st.stop_event.wait(), timeout=None)
                except asyncio.CancelledError:
                    if st.voice:
                        st.voice.stop()
                    raise
                finally:
                    if st.loop == "off":
                        st.now_playing = None
        except asyncio.CancelledError:
            log.info("[%s] Player loop cancelled", guild_id)
        except Exception:
            log.exception("[%s] Player loop crashed", guild_id)
        finally:
            st.player_task = None
            log.info("[%s] Player loop finished", guild_id)

    def ensure_player(self, guild_id: int):
        st = self.state(guild_id)
        if not st.player_task or st.player_task.done():
            st.player_task = asyncio.create_task(self.player_loop(guild_id))

    # ---- Discord lifecycle ----
    async def setup_hook(self):
        await self.tree.sync()
        log.info("Slash commands synced.")

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

bot = MusicBot()

# ---------- Slash Commands ----------
@bot.tree.command(name="join", description="Join your voice channel.")
async def join(interaction: discord.Interaction):
    voice = await bot.ensure_connected(interaction)
    if voice:
        await interaction.response.send_message(f"Joined **{voice.channel}**.", ephemeral=True)

@bot.tree.command(name="leave", description="Leave the voice channel and clear queue.")
async def leave(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    if st.player_task and not st.player_task.done():
        st.player_task.cancel()
    # clear queue
    try:
        while True:
            st.queue.get_nowait()
            st.queue.task_done()
    except asyncio.QueueEmpty:
        pass
    st.now_playing = None
    if st.voice and st.voice.is_connected():
        await st.voice.disconnect(force=False)
        st.voice = None
    await interaction.response.send_message("Left the channel and cleared the queue.")

@bot.tree.command(name="play", description="Play a song from a URL or search query.")
@app_commands.describe(query="YouTube URL or search terms (e.g., 'never gonna give you up')")
async def play(interaction: discord.Interaction, query: str):
    voice = await bot.ensure_connected(interaction)
    if not voice:
        return
    assert interaction.guild
    st = bot.state(interaction.guild.id)

    await interaction.response.defer(thinking=True, ephemeral=False)

    try:
        tracks = await bot.extract_tracks(query, requester_id=interaction.user.id)
    except Exception:
        log.exception("[%s] yt-dlp extraction failed", interaction.guild.id)
        return await interaction.followup.send("❌ Failed to extract audio. Try another link or query.")

    if not tracks:
        return await interaction.followup.send("❌ Nothing found for that query.")

    for t in tracks:
        await st.queue.put(t)

    bot.ensure_player(interaction.guild.id)

    if len(tracks) == 1:
        t = tracks[0]
        embed = discord.Embed(title="Queued", description=f"[{t.title}]({t.url})", color=discord.Color.blurple())
        if t.duration:
            embed.add_field(name="Duration", value=f"{t.duration//60}:{t.duration%60:02d}")
        if t.uploader:
            embed.add_field(name="Channel", value=t.uploader, inline=True)
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"✅ Added **{len(tracks)}** tracks to the queue.")

@bot.tree.command(name="skip", description="Skip the current track.")
async def skip(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    if not st.voice or not st.voice.is_connected() or not st.voice.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    st.voice.stop()
    await interaction.response.send_message("⏭️ Skipped.")

@bot.tree.command(name="pause", description="Pause playback.")
async def pause(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    if not st.voice or not st.voice.is_connected() or not st.voice.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    st.voice.pause()
    await interaction.response.send_message("⏸️ Paused.")

@bot.tree.command(name="resume", description="Resume playback.")
async def resume(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    if not st.voice or not st.voice.is_connected() or not st.voice.is_paused():
        return await interaction.response.send_message("Nothing to resume.", ephemeral=True)
    st.voice.resume()
    await interaction.response.send_message("▶️ Resumed.")

@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    # Clear queue
    try:
        while True:
            st.queue.get_nowait()
            st.queue.task_done()
    except asyncio.QueueEmpty:
        pass
    st.now_playing = None
    if st.voice and (st.voice.is_playing() or st.voice.is_paused()):
        st.voice.stop()
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

@bot.tree.command(name="nowplaying", description="Show the currently playing track.")
async def nowplaying(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    t = st.now_playing
    if not t:
        return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
    embed = discord.Embed(title="Now Playing", description=f"[{t.title}]({t.url})", color=discord.Color.green())
    if t.duration:
        embed.add_field(name="Duration", value=f"{t.duration//60}:{t.duration%60:02d}")
    if t.uploader:
        embed.add_field(name="Channel", value=t.uploader, inline=True)
    if t.thumbnail:
        embed.set_thumbnail(url=t.thumbnail)
    embed.add_field(name="Volume", value=f"{st.volume}%", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="queue", description="Show up to the next 20 songs in the queue.")
async def queue_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)

    # Peek queue (drain to temp then put back)
    temp: list[Track] = []
    try:
        while True and len(temp) < 50:
            temp.append(st.queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    for x in temp:
        st.queue.put_nowait(x)
    items = temp

    lines = []
    for i, t in enumerate(items[:20], start=1):
        d = f" ({t.duration//60}:{t.duration%60:02d})" if t.duration else ""
        lines.append(f"**{i}.** [{t.title}]({t.url}){d}")
    desc = "\n".join(lines) if lines else "_Queue is empty._"
    embed = discord.Embed(title="Queue", description=desc, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove", description="Remove a song from the queue by its position (from /queue).")
@app_commands.describe(position="1-based index as shown in /queue")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)

    temp: list[Track] = []
    try:
        while True:
            temp.append(st.queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    if position > len(temp):
        for x in temp:
            st.queue.put_nowait(x)
        return await interaction.response.send_message("Invalid position.", ephemeral=True)
    removed = temp.pop(position - 1)
    for x in temp:
        st.queue.put_nowait(x)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue.")

@bot.tree.command(name="shuffle", description="Shuffle the queue.")
async def shuffle(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    temp: list[Track] = []
    try:
        while True:
            temp.append(st.queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    import random
    random.shuffle(temp)
    for x in temp:
        st.queue.put_nowait(x)
    await interaction.response.send_message("🔀 Shuffled the queue.")

@bot.tree.command(name="volume", description="Set playback volume (0-150%).")
@app_commands.describe(percent="Volume percent (0-150). Default 80.")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    st.volume = int(percent)
    # If playing, restart current transformer by stopping; loop will restart
    if st.voice and (st.voice.is_playing() or st.voice.is_paused()) and st.now_playing:
        st.voice.stop()
    await interaction.response.send_message(f"🔊 Volume set to **{st.volume}%**.")

@bot.tree.command(name="loop", description="Set loop mode: off, one (repeat current), all (repeat queue).")
@app_commands.describe(mode="Loop mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="one", value="one"),
    app_commands.Choice(name="all", value="all"),
])
async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    st.loop = mode.value  # type: ignore
    await interaction.response.send_message(f"🔁 Loop set to **{st.loop}**.")

# ---------- Entrypoint ----------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set. Put it in your environment or .env file.")
    else:
        bot.run(DISCORD_TOKEN)
