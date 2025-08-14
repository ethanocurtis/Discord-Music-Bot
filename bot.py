#!/usr/bin/env python3
# utila-music-bot / bot.py
# Author: Your Name
# License: MIT
#
# Production-ready Discord music bot using discord.py voice + yt-dlp + ffmpeg
# Python 3.11+
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import enum
import logging
import os
import re
import signal
import sys
import time
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Optional: load .env if present (local dev)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ---- Config via ENV ----
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEV_GUILDS = [int(x) for x in os.getenv("DEV_GUILDS", "").split(",") if x.strip().isdigit()]
DECORATOR_GUILDS = [discord.Object(id=g) for g in DEV_GUILDS]
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
IDLE_DISCONNECT_MINUTES = int(os.getenv("IDLE_DISCONNECT_MINUTES", "5"))

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN not set. See .env.example.", file=sys.stderr)
    sys.exit(1)

# Logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("utila-music-bot")

# Import yt_dlp lazily (module import is heavy)
import yt_dlp  # type: ignore

# FFmpeg args centralized
try:
    from audio.ffmpeg_opts import FFMPEG_BASE_OPTS
except Exception:
    FFMPEG_BASE_OPTS = [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-vn",
        "-af", "aresample=async=1:min_hard_comp=0.100000:first_pts=0",
    ]

YDL_OPTS_BASE: Dict[str, Any] = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": False,  # allow playlist if explicit URL; we detect & disable for search
    "quiet": True,
    "skip_download": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "timeout": 15,
    "retries": 3,
    "extract_flat": False,
    "cachedir": False,
    "restrictfilenames": True,
}

YDL_OPTS_SEARCH = {**YDL_OPTS_BASE, "noplaylist": True}

PROGRESS_BAR_BLOCKS = 16  # textual progress bar blocks
VOLUME_MIN = 0
VOLUME_MAX = 200
IDLE_SECONDS = IDLE_DISCONNECT_MINUTES * 60


class LoopMode(enum.Enum):
    OFF = 0
    TRACK = 1
    QUEUE = 2


@dataclass
class Track:
    title: str
    url: str  # webpage URL
    stream_url: str  # direct audio URL (if available) or webpage URL for ffmpeg to handle
    duration: Optional[int]  # seconds
    uploader: Optional[str]
    requester_id: int
    source: str = "YouTube"
    thumbnail: Optional[str] = None


@dataclass
class GuildState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue: List[Track] = field(default_factory=list)
    loop_mode: LoopMode = LoopMode.OFF
    volume: int = 100  # 0..200 (percent)
    voice: Optional[discord.VoiceClient] = None
    now_playing: Optional[Track] = None
    player_task: Optional[asyncio.Task] = None
    progress_task: Optional[asyncio.Task] = None
    last_np_message_id: Optional[int] = None
    last_np_channel_id: Optional[int] = None
    idle_since: Optional[float] = None  # monotonic
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def is_idle(self) -> bool:
        if self.now_playing or self.queue:
            return False
        if self.idle_since is None:
            return False
        return (time.monotonic() - self.idle_since) > IDLE_SECONDS

    def mark_idle(self) -> None:
        self.idle_since = time.monotonic()

    def clear_idle(self) -> None:
        self.idle_since = None


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.message_content = False
        intents.reactions = True
        intents.members = False

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            application_id=None,
        )
        # Use the built-in CommandTree (self.tree) — do not replace it.
        self.guild_states: Dict[int, GuildState] = {}

        # Debounce map for NP embed edits: guild_id -> last_edit_ts
        self._embed_edit_ts: Dict[int, float] = {}

    # ---------- Helpers ----------

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildState()
        return self.guild_states[guild_id]

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient:
        # Validate context
        assert interaction.guild and interaction.user
        state = self.get_state(interaction.guild.id)
        
        # User must be in a voice channel
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            raise app_commands.CheckFailure("You must be in a voice channel to use this command.")
        
        channel = interaction.user.voice.channel
        vc = state.voice
        
        # Reuse or move existing connection when possible
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            vc = await channel.connect(self_deaf=True)
        
        state.voice = vc
        return vc
    async def check_same_channel(self, interaction: discord.Interaction) -> None:
        # Skip for owners
        if interaction.user and interaction.user.id in OWNER_IDS:
            return
        guild = interaction.guild
        if not guild:
            raise commands.CommandError("Guild missing.")
        state = self.get_state(guild.id)
        if state.voice and state.voice.channel:
            if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
                raise commands.CommandError("You must join my voice channel to control playback.")
            if interaction.user.voice.channel != state.voice.channel:
                raise commands.CommandError("You must be in the same voice channel as the bot.")

    def _can_edit_embed(self, guild_id: int) -> bool:
        now = time.monotonic()
        last = self._embed_edit_ts.get(guild_id, 0.0)
        if now - last >= 1.0:
            self._embed_edit_ts[guild_id] = now
            return True
        return False

    # ---------- yt-dlp search/resolve ----------
    async def resolve_query(self, query: str, requester_id: int) -> List[Track]:
        loop = asyncio.get_running_loop()

        def _extract() -> List[Track]:
            opts = YDL_OPTS_SEARCH if not self._looks_like_url(query) else YDL_OPTS_BASE
            # Disable playlist unless URL explicitly is a playlist
            if not self._looks_like_url(query) or ("list=" not in query and "playlist" not in query):
                opts = {**opts, "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            if info is None:
                return []
            entries = []
            if "entries" in info and info.get("_type") == "playlist":
                for e in info["entries"] or []:
                    if e:
                        entries.append(e)
            else:
                entries.append(info)

            tracks: List[Track] = []
            for e in entries:
                title = e.get("title") or "Unknown Title"
                webpage_url = e.get("webpage_url") or e.get("url") or query
                # Prefer direct URL if provided; ffmpeg can also handle webpage URL
                stream_url = e.get("url") or webpage_url
                duration = e.get("duration")
                uploader = e.get("uploader") or e.get("channel")
                thumbnail = e.get("thumbnail")
                extractor = e.get("extractor_key") or "YouTube"
                tracks.append(
                    Track(
                        title=title,
                        url=webpage_url,
                        stream_url=stream_url,
                        duration=duration,
                        uploader=uploader,
                        requester_id=requester_id,
                        source=extractor,
                        thumbnail=thumbnail,
                    )
                )
            return tracks

        return await loop.run_in_executor(None, _extract)

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        return bool(re.match(r"https?://", text.strip()))

    # ---------- Embeds ----------
    def build_now_playing_embed(self, guild_id: int) -> Optional[discord.Embed]:
        state = self.guild_states.get(guild_id)
        if not state or not state.now_playing:
            return None
        t = state.now_playing
        pos = self._current_position_seconds(state)
        dur = t.duration or 0
        bar = self._progress_bar(pos, dur)

        desc_lines = [
            f"**Uploader:** {t.uploader or 'Unknown'}",
            f"**Duration:** {self._fmt_time(dur)}",
            f"**Requested by:** <@{t.requester_id}>",
            f"**Loop:** {state.loop_mode.name}",
            f"**Volume:** {state.volume}%",
            f"**Queue:** {len(state.queue)}",
            "",
            f"{bar}  `{self._fmt_time(pos)} / {self._fmt_time(dur)}`",
        ]
        embed = discord.Embed(
            title=f"Now Playing — {t.title}",
            url=t.url,
            description="\n".join(desc_lines),
            color=discord.Color.blurple(),
        )
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        embed.set_footer(text="utila-music-bot")
        return embed

    @staticmethod
    def _progress_bar(pos: int, dur: int) -> str:
        if dur <= 0:
            return "■" * 2 + "▁" * (PROGRESS_BAR_BLOCKS - 2)
        filled = max(1, min(PROGRESS_BAR_BLOCKS, int((pos / dur) * PROGRESS_BAR_BLOCKS)))
        return "▇" * filled + "▁" * (PROGRESS_BAR_BLOCKS - filled)

    @staticmethod
    def _fmt_time(s: Optional[int]) -> str:
        if s is None or s < 0:
            return "??:??"
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:01d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    def _current_position_seconds(self, state: GuildState) -> int:
        if not state.voice or not state.voice.is_playing():
            return 0
        src = getattr(state.voice.source, "_start_ts", None)
        if src is None:
            return 0
        return int(time.monotonic() - src)

    # ---------- Playback ----------

    async def player_loop(self, guild_id: int, channel) -> None:  # type: ignore
        """Playback loop per guild; runs inside a task."""
        state = self.get_state(guild_id)
        log.info(f"[{guild_id}] Player loop started")
        try:
            while True:
                async with state.lock:
                    if state.now_playing is None:
                        if not state.queue:
                            # Idle
                            if state.idle_since is None:
                                state.mark_idle()
                            if state.is_idle():
                                # Leave voice if idle too long
                                if state.voice and state.voice.is_connected():
                                    await state.voice.disconnect(force=False)
                                    state.voice = None
                                    log.info(f"[{guild_id}] Disconnected due to idle")
                                break
                            # Check again in a bit
                            await asyncio.sleep(2)
                            continue
                        # Pop next track
                        track = state.queue.pop(0)
                        state.now_playing = track
                        state.clear_idle()

                # Start playing current track
                ok = await self._play_track(guild_id, channel)
                if not ok:
                    # Skip to next or stop
                    async with state.lock:
                        state.now_playing = None
                    continue

                # Wait until playback finished
                await self._wait_playback_finish(guild_id)

                # On finish: decide next track based on loop mode
                async with state.lock:
                    finished = state.now_playing
                    state.now_playing = None

                    if state.loop_mode == LoopMode.TRACK and finished:
                        state.queue.insert(0, finished)
                    elif state.loop_mode == LoopMode.QUEUE and finished:
                        state.queue.append(finished)

        except asyncio.CancelledError:
            log.info(f"[{guild_id}] Player loop cancelled")
            raise
        except Exception as e:
            log.exception(f"[{guild_id}] Player loop error: {e}")
        finally:
            # cleanup
            async with state.lock:
                if state.progress_task:
                    state.progress_task.cancel()
                if state.voice and state.voice.is_playing():
                    state.voice.stop()
                state.player_task = None
            log.info(f"[{guild_id}] Player loop ended")

    async def _play_track(self, guild_id: int, channel) -> bool:  # type: ignore
        state = self.get_state(guild_id)
        track = state.now_playing
        if not track:
            return False
        if not state.voice or not state.voice.is_connected():
            log.warning(f"[{guild_id}] No voice connected; cannot play")
            return False

        try:
            # Build source without filter; control gain via PCMVolumeTransformer
            src = discord.FFmpegPCMAudio(
                source=track.stream_url,
                before_options=" ".join(FFMPEG_BASE_OPTS),
                options="-vn",
            )
            src = discord.PCMVolumeTransformer(src, volume=state.volume / 100.0)
            # Progress tracking
            setattr(src, "_start_ts", time.monotonic())

            # Stop existing first, then play
            if state.voice.is_playing() or state.voice.is_paused():
                state.voice.stop()
            state.stop_event.clear()
            state.voice.play(src, after=lambda e: state.stop_event.set())
            return True
        except Exception as e:
            log.exception(f"[{}] play failed: {e}".format(guild_id))
            return False
    async def _wait_playback_finish(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        # Wait until source stops
        while True:
            if not state.voice or not state.voice.is_connected() or not state.voice.is_playing():
                break
            try:
                await asyncio.wait_for(state.stop_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                continue

    async def _post_or_update_np(self, guild_id: int, channel, force_new: bool) -> None:  # type: ignore
        state = self.get_state(guild_id)
        embed = self.build_now_playing_embed(guild_id)
        if not embed:
            return

        try:
            if state.last_np_message_id and state.last_np_channel_id and getattr(channel, "id", None) == state.last_np_channel_id and not force_new:
                # Edit existing with debounce
                if self._can_edit_embed(guild_id):
                    msg = await channel.fetch_message(state.last_np_message_id)
                    await msg.edit(embed=embed)
                return
        except discord.NotFound:
            # message disappeared; recreate
            state.last_np_message_id = None

        msg = await channel.send(embed=embed)
        state.last_np_message_id = msg.id
        state.last_np_channel_id = getattr(channel, "id", None)
        # Attach control reactions once
        for emoji in ["⏯", "⏭", "⏹", "🔁", "🔀", "🔉", "🔊", "⏮"]:
            try:
                await msg.add_reaction(emoji)
            except discord.Forbidden:
                pass  # Missing perms

    async def _progress_updater(self, guild_id: int, channel) -> None:  # type: ignore
        try:
            while True:
                await asyncio.sleep(1.0)
                await self._post_or_update_np(guild_id, channel, force_new=False)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception(f"[{guild_id}] progress updater failed")

    async def stop_playback(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        async with state.lock:
            state.queue.clear()
            state.loop_mode = LoopMode.OFF
            if state.voice and (state.voice.is_playing() or state.voice.is_paused()):
                state.voice.stop()
            state.now_playing = None
            if state.progress_task:
                state.progress_task.cancel()
                state.progress_task = None
    # ---------- Command Checks ----------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Basic per-command logging
        try:
            user = f"{interaction.user} ({interaction.user.id})" if interaction.user else "unknown"
            log.info(f"Command: /{interaction.command.name if interaction.command else '?'} by {user} in {interaction.guild_id}")
        except Exception:
            pass
        return True

    # ---------- Events ----------
    async def setup_hook(self) -> None:
        # Register commands
        self._register_commands()
        # Start background health task now that loop is running
        self.healthbeat.start()
        # Sync behavior: guild for dev, else global
        if DEV_GUILDS:
            for gid in DEV_GUILDS:
                try:
                    await self.tree.sync(guild=discord.Object(id=gid))
                    log.info(f"Synchronized commands to guild {gid}")
                except Exception:
                    log.exception(f"Failed to sync to guild {gid}")
        else:
            try:
                await self.tree.sync()
                log.info("Synchronized global commands")
            except Exception:
                log.exception("Failed to sync global commands")

    async def on_ready(self) -> None:
        log.info(f"Logged in as {self.user} (ID: {self.user and self.user.id})")
        await self.change_presence(activity=discord.Game(name="music • /play"))

    
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        # If bot got disconnected from VC unexpectedly but we still have a queue/track, try to reconnect
        if self.user and member.id == self.user.id:
            guild = member.guild
            state = self.guild_states.get(guild.id)
            if not state:
                return
            # If we lost the voice connection
            if before.channel and not after.channel:
                if state.queue or state.now_playing:
                    # Try to reconnect to the last known channel if possible
                    try:
                        # Prefer user's current channel if requester still there; fallback to before.channel
                        target = before.channel
                        if target:
                            state.voice = await target.connect(self_deaf=True)
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass
    

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Cleanup state
        state = self.guild_states.pop(guild.id, None)
        if state and state.voice and state.voice.is_connected():
            await state.voice.disconnect(force=False)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return  # ignore self
        guild_id = payload.guild_id
        if not guild_id:
            return
        state = self.guild_states.get(guild_id)
        if not state or state.last_np_message_id != payload.message_id:
            return
        # Only allow humans (not bots)
        if payload.member and payload.member.bot:
            return
        # Must be in same voice channel to control
        try:
            guild = self.get_guild(guild_id)
            if not guild:
                return
            channel = guild.get_channel(state.last_np_channel_id) if state.last_np_channel_id else None
            if not channel or not hasattr(channel, "fetch_message"):
                return
            user_member = guild.get_member(payload.user_id)
            if not user_member or not state.voice or not user_member.voice or user_member.voice.channel != state.voice.channel:
                return

            emoji = str(payload.emoji)
            await self._handle_reaction(guild_id, emoji, channel)
            # Try to remove the user's reaction to keep the panel tidy
            try:
                msg = await channel.fetch_message(state.last_np_message_id)
                await msg.remove_reaction(payload.emoji, user_member)
            except Exception:
                pass
        except Exception:
            log.exception("Reaction handler error")

    async def _handle_reaction(self, guild_id: int, emoji: str, channel) -> None:  # type: ignore
        state = self.get_state(guild_id)
        async with state.lock:
            if emoji == "⏯":  # pause/resume
                if state.voice and state.voice.is_playing():
                    state.voice.pause()
                elif state.voice and state.voice.is_paused():
                    state.voice.resume()
            elif emoji == "⏭":  # skip
                if state.voice and (state.voice.is_playing() or state.voice.is_paused()):
                    state.voice.stop()
            elif emoji == "⏹":  # stop
                await self.stop_playback(guild_id)
            elif emoji == "🔁":  # loop toggle (OFF -> TRACK -> QUEUE -> OFF)
                if state.loop_mode == LoopMode.OFF:
                    state.loop_mode = LoopMode.TRACK
                elif state.loop_mode == LoopMode.TRACK:
                    state.loop_mode = LoopMode.QUEUE
                else:
                    state.loop_mode = LoopMode.OFF
            elif emoji == "🔀":  # shuffle
                import random
                random.shuffle(state.queue)
            elif emoji == "🔉":  # volume down
                state.volume = max(VOLUME_MIN, state.volume - 10)
                await self._restart_with_new_volume(guild_id, channel)
            elif emoji == "🔊":  # volume up
                state.volume = min(VOLUME_MAX, state.volume + 10)
                await self._restart_with_new_volume(guild_id, channel)
            elif emoji == "⏮":  # replay current track from start
                await self._seek_to(guild_id, 0, channel)
        await self._post_or_update_np(guild_id, channel, force_new=False)

    async def _seek_to(self, guild_id: int, seconds: int, channel) -> None:  # type: ignore
        state = self.get_state(guild_id)
        if not state.now_playing or not state.voice:
            return
        track = state.now_playing
        vol = state.volume / 100.0
        try:
            src = discord.FFmpegPCMAudio(
                source=track.stream_url,
                before_options=" ".join(FFMPEG_BASE_OPTS + ["-ss", str(max(0, seconds))]
                options="-vn",
            )
            src = discord.PCMVolumeTransformer(src, volume=vol)
            setattr(src, "_start_ts", time.monotonic() - seconds)
            state.voice.stop()
            state.stop_event.clear()
            state.voice.play(src, after=lambda e: state.stop_event.set())
        except Exception:
            log.exception(f"[{guild_id}] seek failed")
        await self._post_or_update_np(guild_id, channel, force_new=False)
    async def _restart_with_new_volume(self, guild_id: int, channel) -> None:  # type: ignore
        state = self.get_state(guild_id)
        if state and state.voice and isinstance(state.voice.source, discord.PCMVolumeTransformer):
            state.voice.source.volume = state.volume / 100.0
            if channel is not None:
                await self._post_or_update_np(guild_id, channel, force_new=False)
            return
        # Fallback: restart from current position if we don't have a transformer yet
        if not (state.voice and state.now_playing):
            return
        pos = self._current_position_seconds(state)
        await self._seek_to(guild_id, max(0, pos), channel)
    # ---------- Tasks ----------
    @tasks.loop(minutes=2.0)
    async def healthbeat(self) -> None:
        log.debug("healthbeat ok")

    # ---------- Command Registration ----------
    def _register_commands(self) -> None:
        @self.tree.command(
            name="play",
            description="Play a YouTube/SoundCloud/HTTP URL or search query.",
            guilds=DECORATOR_GUILDS,
        )
        @app_commands.describe(query_or_url="YouTube URL or search terms")
        async def play(interaction: discord.Interaction, query_or_url: str) -> None:
            await interaction.response.defer(thinking=True)
            if not interaction.guild:
                await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
                return
            state = self.get_state(interaction.guild.id)
            await self.ensure_voice(interaction)

            tracks = await self.resolve_query(query_or_url, requester_id=interaction.user.id)
            if not tracks:
                await interaction.followup.send("No results found.", ephemeral=True)
                return

            chosen = tracks[0]

            # Choose a channel to post NP updates
            channel = interaction.channel
            if channel is None and interaction.guild:
                channel = interaction.guild.system_channel
            if channel is None and interaction.guild:
                for ch in interaction.guild.text_channels:
                    if ch.permissions_for(interaction.guild.me).send_messages:
                        channel = ch
                        break
            if channel is None:
                await interaction.followup.send("Cannot determine a channel for updates (need permission to send messages).", ephemeral=True)
                return

            async with state.lock:
                state.queue.append(chosen)
                msg = f"Enqueued **{chosen.title}** (`{self._fmt_time(chosen.duration or 0)}`) from {chosen.source}."
            await interaction.followup.send(msg)

            # Start player if idle
            async with state.lock:
                if not state.player_task or state.player_task.done():
                    state.player_task = asyncio.create_task(self.player_loop(interaction.guild.id, channel))

        @self.tree.command(name="pause", description="Pause playback.", guilds=DECORATOR_GUILDS)
        async def pause(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if state.voice and state.voice.is_playing():
                state.voice.pause()
                await interaction.response.send_message("⏸️ Paused.")
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        @self.tree.command(name="resume", description="Resume playback.", guilds=DECORATOR_GUILDS)
        async def resume(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if state.voice and state.voice.is_paused():
                state.voice.resume()
                await interaction.response.send_message("▶️ Resumed.")
            else:
                await interaction.response.send_message("Nothing is paused.", ephemeral=True)

        @self.tree.command(name="skip", description="Skip the current track.", guilds=DECORATOR_GUILDS)
        async def skip(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if state.voice and (state.voice.is_playing() or state.voice.is_paused()):
                state.voice.stop()
                await interaction.response.send_message("⏭️ Skipped.")
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        @self.tree.command(name="stop", description="Stop playback and clear the queue.", guilds=DECORATOR_GUILDS)
        async def stop(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            await self.stop_playback(interaction.guild.id)  # type: ignore
            await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

        @self.tree.command(name="queue", description="Show the queue.", guilds=DECORATOR_GUILDS)
        async def queue(interaction: discord.Interaction) -> None:
            state = self.get_state(interaction.guild.id)  # type: ignore
            if not state.queue:
                await interaction.response.send_message("Queue is empty.")
                return
            lines = []
            for i, t in enumerate(state.queue, 1):
                lines.append(f"`{i:02d}.` {t.title} — `{self._fmt_time(t.duration or 0)}` ({t.source})")
            embed = discord.Embed(title="Queue", description="\n".join(lines[:20]), color=discord.Color.blurple())
            if len(lines) > 20:
                embed.set_footer(text=f"And {len(lines)-20} more...")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="np", description="Show the currently playing track.", guilds=DECORATOR_GUILDS)
        async def np(interaction: discord.Interaction) -> None:
            embed = self.build_now_playing_embed(interaction.guild.id)  # type: ignore
            if not embed:
                await interaction.response.send_message("Nothing is playing.")
                return
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="volume", description="Set volume (0-200%).", guilds=DECORATOR_GUILDS)
        @app_commands.describe(percent="Volume percent (0-200)")
        async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, VOLUME_MIN, VOLUME_MAX]) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            state.volume = percent
            if interaction.channel:
                await self._restart_with_new_volume(interaction.guild.id, interaction.channel)  # type: ignore
            await interaction.response.send_message(f"🔊 Volume set to **{percent}%**.")

        @self.tree.command(name="clear", description="Clear the queue.", guilds=DECORATOR_GUILDS)
        async def clear(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            n = len(state.queue)
            state.queue.clear()
            await interaction.response.send_message(f"🧹 Cleared {n} tracks from the queue.")

        @self.tree.command(name="shuffle", description="Shuffle the queue.", guilds=DECORATOR_GUILDS)
        async def shuffle(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            import random
            state = self.get_state(interaction.guild.id)  # type: ignore
            random.shuffle(state.queue)
            await interaction.response.send_message("🔀 Shuffled the queue.")

        @self.tree.command(name="remove", description="Remove a track at index.", guilds=DECORATOR_GUILDS)
        @app_commands.describe(index="1-based index in the queue")
        async def remove(interaction: discord.Interaction, index: app_commands.Range[int, 1, 10_000]) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if index < 1 or index > len(state.queue):
                await interaction.response.send_message("Index out of range.", ephemeral=True)
                return
            t = state.queue.pop(index - 1)
            await interaction.response.send_message(f"Removed **{t.title}** from the queue.")

        @self.tree.command(name="move", description="Move a track in the queue.", guilds=DECORATOR_GUILDS)
        @app_commands.describe(from_index="From index (1-based)", to_index="To index (1-based)")
        async def move(interaction: discord.Interaction, from_index: int, to_index: int) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            n = len(state.queue)
            if not (1 <= from_index <= n and 1 <= to_index <= n):
                await interaction.response.send_message("Index out of range.", ephemeral=True)
                return
            item = state.queue.pop(from_index - 1)
            state.queue.insert(to_index - 1, item)
            await interaction.response.send_message(f"Moved **{item.title}** to position {to_index}.")

        @self.tree.command(name="seek", description="Seek within the current track (mm:ss).", guilds=DECORATOR_GUILDS)
        @app_commands.describe(timestamp="Format mm:ss (or h:mm:ss)")
        async def seek(interaction: discord.Interaction, timestamp: str) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if not state.now_playing:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return
            secs = _parse_timestamp(timestamp)
            if secs < 0 or (state.now_playing.duration and secs > state.now_playing.duration):
                await interaction.response.send_message("Seek target out of range.", ephemeral=True)
                return
            if interaction.channel:
                await self._seek_to(interaction.guild.id, secs, interaction.channel)  # type: ignore
                await interaction.response.send_message(f"⏩ Sought to `{timestamp}`.")
            else:
                await interaction.response.send_message("Cannot determine text channel to update.", ephemeral=True)

        @self.tree.command(name="loop", description="Set loop mode: off, track, queue.", guilds=DECORATOR_GUILDS)
        @app_commands.choices(mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="track", value="track"),
            app_commands.Choice(name="queue", value="queue"),
        ])
        @app_commands.describe(mode="off | track | queue")
        async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            m = mode.value.lower()
            if m == "off":
                state.loop_mode = LoopMode.OFF
            elif m == "track":
                state.loop_mode = LoopMode.TRACK
            elif m == "queue":
                state.loop_mode = LoopMode.QUEUE
            else:
                await interaction.response.send_message("Invalid mode. Use off|track|queue.", ephemeral=True)
                return
            await interaction.response.send_message(f"🔁 Loop set to **{state.loop_mode.name}**.")

        # ---- Extras ----
        @self.tree.command(name="remove_dupes", description="Remove duplicate URLs from the queue.", guilds=DECORATOR_GUILDS)
        async def remove_dupes(interaction: discord.Interaction) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            seen = set()
            newq = []
            removed = 0
            for t in state.queue:
                key = t.url
                if key in seen:
                    removed += 1
                else:
                    seen.add(key)
                    newq.append(t)
            state.queue = newq
            await interaction.response.send_message(f"Removed {removed} duplicates.")

        @self.tree.command(name="jump", description="Jump to a queue index and start playing it.", guilds=DECORATOR_GUILDS)
        async def jump(interaction: discord.Interaction, index: int) -> None:
            await self.check_same_channel(interaction)
            state = self.get_state(interaction.guild.id)  # type: ignore
            if not (1 <= index <= len(state.queue)):
                await interaction.response.send_message("Index out of range.", ephemeral=True)
                return
            # Move that track to front and stop current to trigger next
            async with state.lock:
                chosen = state.queue.pop(index - 1)
                state.queue.insert(0, chosen)
                if state.voice and (state.voice.is_playing() or state.voice.is_paused()):
                    state.voice.stop()
            await interaction.response.send_message(f"Jumped to **{chosen.title}**.")

    # ---------- Error handling ----------
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = "An error occurred."
        if isinstance(error, app_commands.CheckFailure) or isinstance(error, commands.CommandError):
            msg = str(error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        log.exception(f"Command error: {error}")


def _parse_timestamp(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    return -1


# ---- Graceful shutdown in Docker ----
bot: MusicBot

def _handle_sigterm():
    try:
        loop = asyncio.get_event_loop()
        for gi, state in list(bot.guild_states.items()):
            if state.voice and state.voice.is_connected():
                loop.create_task(state.voice.disconnect(force=False))
    except Exception:
        pass
    finally:
        asyncio.get_event_loop().stop()

def main() -> None:
    global bot
    bot = MusicBot()
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    except Exception:
        pass
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()