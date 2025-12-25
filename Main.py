# ==============================
# JJ Bot (Roles + Moderation + Music)
# Paste this over your whole file
# ==============================

# ---------- LOGGING SETUP (MUST BE FIRST) ----------
import logging
from logging import DEBUG
import os
import asyncio
import shutil
from collections import deque

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp
import discord
import os

if not discord.opus.is_loaded():
    discord.opus.load_opus("/opt/homebrew/lib/libopus.dylib")

LOG_FILE = "discord.log"

logging.basicConfig(
    level=DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a"),
        logging.StreamHandler(),
    ],
)

log = logging.getLogger("bot")

# ---------- ENV ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ---------- INTENTS ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ---------- BOT ----------
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- ROLES ----------
SECRET_ROLE = "Children"
SECRET_ROLE2 = "OGS"

# ---------- FFmpeg / Opus SETUP ----------
def find_ffmpeg() -> str:
    # 1) Allow override with env var if you want:
    #    export FFMPEG_BIN="/opt/homebrew/bin/ffmpeg"
    env_bin = os.getenv("FFMPEG_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    # 2) Normal PATH lookup
    ff = shutil.which("ffmpeg")
    if ff:
        return ff

    # 3) Common Homebrew locations
    candidates = [
        "/opt/homebrew/bin/ffmpeg",  # Apple Silicon
        "/usr/local/bin/ffmpeg",     # Intel
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    return ""


FFMPEG_BIN = find_ffmpeg()
if FFMPEG_BIN:
    log.info("Using FFmpeg at: %s", FFMPEG_BIN)
else:
    log.warning("FFmpeg was NOT found. Music playback will fail until FFmpeg is available in PATH or FFMPEG_BIN is set.")


def try_load_opus():
    # discord.py needs Opus for voice. On macOS, this can come from Homebrew "opus".
    # You can install with: brew install opus
    try:
        if not discord.opus.is_loaded():
            discord.opus.load_opus("libopus.0.dylib")  # common name on mac
        log.info("Opus loaded: %s", discord.opus.is_loaded())
    except Exception as e:
        log.warning("Could not explicitly load Opus (might still work if already available). Error: %s", e)


try_load_opus()

# ---------- MUSIC (yt-dlp) ----------
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


class Track:
    def __init__(self, title: str, stream_url: str, webpage_url: str, requester: discord.Member):
        self.title = title
        self.stream_url = stream_url
        self.webpage_url = webpage_url
        self.requester = requester


class MusicState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.next_event = asyncio.Event()
        self.player_task: asyncio.Task | None = None
        self.text_channel: discord.TextChannel | None = None

    def ensure_task(self, guild: discord.Guild):
        if self.player_task and not self.player_task.done():
            return
        self.player_task = asyncio.create_task(self.player_loop(guild))

    async def player_loop(self, guild: discord.Guild):
        log.info("Music player loop started for guild=%s", guild.id)

        while True:
            if not self.queue:
                self.current = None
                await asyncio.sleep(0.5)
                continue

            vc = guild.voice_client
            if not vc or not vc.is_connected():
                log.info("No voice client; stopping player loop guild=%s", guild.id)
                self.current = None
                return

            self.current = self.queue.popleft()
            self.next_event.clear()

            if not FFMPEG_BIN:
                if self.text_channel:
                    await self.text_channel.send("❌ FFmpeg not found. Install it and restart the bot.")
                log.error("FFmpeg missing; cannot play.")
                self.current = None
                await asyncio.sleep(1)
                continue

            try:
                source = discord.FFmpegPCMAudio(
                    self.current.stream_url,
                    executable=FFMPEG_BIN,
                    **FFMPEG_OPTS,
                )
            except Exception as e:
                log.exception("Failed to create FFmpeg source: %s", e)
                if self.text_channel:
                    await self.text_channel.send(f"❌ Could not start FFmpeg for this track.\n`{e}`")
                self.current = None
                continue

            def after_play(err: Exception | None):
                if err:
                    log.warning("Playback error: %s", err)
                bot.loop.call_soon_threadsafe(self.next_event.set)

            try:
                vc.play(source, after=after_play)
            except discord.opus.OpusNotLoaded:
                log.exception("Opus not loaded.")
                if self.text_channel:
                    await self.text_channel.send(
                        "❌ Opus is not loaded. On macOS run:\n"
                        "`brew install opus`\n"
                        "Then restart your bot."
                    )
                self.current = None
                continue
            except Exception as e:
                log.exception("vc.play failed: %s", e)
                if self.text_channel:
                    await self.text_channel.send(f"❌ Could not play that track.\n`{e}`")
                self.current = None
                continue

            if self.text_channel:
                await self.text_channel.send(
                    f"🎶 Now playing: **{self.current.title}** (requested by {self.current.requester.mention})"
                )
            log.info("Now playing guild=%s: %s", guild.id, self.current.title)

            await self.next_event.wait()


music_states: dict[int, MusicState] = {}


def get_state(guild_id: int) -> MusicState:
    state = music_states.get(guild_id)
    if not state:
        state = MusicState()
        music_states[guild_id] = state
    return state


def extract_info(search: str) -> dict:
    return ytdl.extract_info(search, download=False)


async def fetch_tracks(search: str, requester: discord.Member) -> list[Track]:
    data = await asyncio.to_thread(extract_info, search)

    tracks: list[Track] = []

    # playlist/search results
    if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], list):
        for entry in data["entries"]:
            if not entry:
                continue
            title = entry.get("title") or "Unknown title"
            stream_url = entry.get("url")
            webpage_url = entry.get("webpage_url") or entry.get("original_url") or ""
            if not stream_url:
                continue
            tracks.append(Track(title, stream_url, webpage_url, requester))
        return tracks

    # single
    title = data.get("title") or "Unknown title"
    stream_url = data.get("url")
    webpage_url = data.get("webpage_url") or data.get("original_url") or ""
    if stream_url:
        tracks.append(Track(title, stream_url, webpage_url, requester))

    return tracks


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("You must be in a voice channel first.")

    channel = ctx.author.voice.channel
    vc = ctx.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel != channel:
            await vc.move_to(channel)
            log.info("Moved voice client to %s", channel)
        return vc

    vc = await channel.connect()
    log.info("Connected voice client to %s", channel)
    return vc


# ---------- EVENTS ----------
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

@bot.event
async def on_member_join(member):
    log.info("Member joined: %s (%s)", member, member.id)
    try:
        await member.send(f"Welcome to the server {member.name}")
    except discord.Forbidden:
        log.warning("Could not DM welcome message to %s", member)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    log.debug("Message from %s in #%s: %s", message.author, message.channel, message.content)

    if "shit" in message.content.lower():
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention} - don’t use that word")
            log.info("Deleted message from %s in #%s", message.author, message.channel)
        except discord.Forbidden:
            log.warning("Missing permissions to delete messages in #%s", message.channel)
        except discord.HTTPException as e:
            log.warning("Failed to delete message: %s", e)

    await bot.process_commands(message)

@bot.event
async def on_command(ctx):
    log.info("Command used: %s | User: %s | Channel: %s", ctx.command, ctx.author, ctx.channel)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("You do not have the required role")
        return

    log.exception("Command error: %s", error)
    await ctx.send(f"❌ {error}")


# ---------- COMMANDS ----------
@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention}")

@bot.command()
async def assign(ctx):
    role = discord.utils.get(ctx.guild.roles, name=SECRET_ROLE)
    if role:
        await ctx.author.add_roles(role)
        log.info("Added role %s to %s", SECRET_ROLE, ctx.author)
        await ctx.send(f"{ctx.author.mention} has been assigned {SECRET_ROLE}")
    else:
        await ctx.send("Role doesn't exist")

@bot.command()
async def remove(ctx):
    role = discord.utils.get(ctx.guild.roles, name=SECRET_ROLE)
    if role:
        await ctx.author.remove_roles(role)
        log.info("Removed role %s from %s", SECRET_ROLE, ctx.author)
        await ctx.send(f"{ctx.author.mention} has had {SECRET_ROLE} removed")
    else:
        await ctx.send("Role doesn't exist")

@bot.command()
async def assign2(ctx):
    role = discord.utils.get(ctx.guild.roles, name=SECRET_ROLE2)
    if role:
        await ctx.author.add_roles(role)
        log.info("Added role %s to %s", SECRET_ROLE2, ctx.author)
        await ctx.send(f"{ctx.author.mention} has been assigned {SECRET_ROLE2}")
    else:
        await ctx.send("Role doesn't exist")

@bot.command()
async def remove2(ctx):
    role = discord.utils.get(ctx.guild.roles, name=SECRET_ROLE2)
    if role:
        await ctx.author.remove_roles(role)
        log.info("Removed role %s from %s", SECRET_ROLE2, ctx.author)
        await ctx.send(f"{ctx.author.mention} has had {SECRET_ROLE2} removed")
    else:
        await ctx.send("Role doesn't exist")

@bot.command()
async def dm(ctx, *, msg):
    await ctx.author.send(f"You said: {msg}")
    log.info("DM sent to %s", ctx.author)

@bot.command()
async def reply(ctx):
    await ctx.reply("This is a reply to your message")

@bot.command()
async def poll(ctx, *, question):
    embed = discord.Embed(title="Poll", description=question)
    poll_message = await ctx.send(embed=embed)
    await poll_message.add_reaction("👍")
    await poll_message.add_reaction("👎")
    log.info("Poll created by %s", ctx.author)

@bot.command()
@commands.has_role(SECRET_ROLE2)
async def secret(ctx):
    await ctx.send("Welcome to the OGS")


# ---------- MUSIC COMMANDS ----------
@bot.command()
async def join(ctx):
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel
    await ensure_voice(ctx)
    state.ensure_task(ctx.guild)
    await ctx.send("✅ Joined voice channel.")

@bot.command()
async def leave(ctx):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("I’m not in a voice channel.")

    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None

    await vc.disconnect()
    await ctx.send("👋 Left voice channel and cleared the queue.")
    log.info("Left voice channel guild=%s", ctx.guild.id)

@bot.command()
async def play(ctx, *, query: str):
    """
    Examples:
      !play never gonna give you up
      !play https://www.youtube.com/watch?v=...
    """
    if not FFMPEG_BIN:
        return await ctx.send(
            "❌ FFmpeg not found by the bot.\n"
            "Fix it by making sure FFmpeg is in PATH for PyCharm OR set:\n"
            "`FFMPEG_BIN=/opt/homebrew/bin/ffmpeg` (Apple Silicon)\n"
            "`FFMPEG_BIN=/usr/local/bin/ffmpeg` (Intel)\n"
            "Then restart."
        )

    vc = await ensure_voice(ctx)

    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel
    state.ensure_task(ctx.guild)

    tracks = await fetch_tracks(query, ctx.author)
    if not tracks:
        return await ctx.send("❌ I couldn’t find anything for that.")

    for t in tracks:
        state.queue.append(t)

    if len(tracks) == 1:
        await ctx.send(f"✅ Queued: **{tracks[0].title}**")
    else:
        await ctx.send(f"✅ Queued **{len(tracks)}** tracks.")

    # If nothing playing, loop will pick up soon
    if not vc.is_playing() and not vc.is_paused():
        pass

@bot.command()
async def now(ctx):
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.send("Nothing is playing right now.")
    await ctx.send(f"🎶 Now playing: **{state.current.title}** (requested by {state.current.requester.mention})")

@bot.command(name="queue")
async def show_queue(ctx):
    state = get_state(ctx.guild.id)
    if not state.queue:
        return await ctx.send("Queue is empty.")

    preview = list(state.queue)[:10]
    desc = "\n".join([f"{i+1}. {t.title} (by {t.requester.display_name})" for i, t in enumerate(preview)])
    await ctx.send(f"📜 Up next:\n{desc}")

@bot.command()
async def skip(ctx):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("I’m not in a voice channel.")
    if not vc.is_playing():
        return await ctx.send("Nothing is playing.")
    vc.stop()
    await ctx.send("⏭️ Skipped.")
    log.info("Skipped track guild=%s", ctx.guild.id)

@bot.command()
async def pause(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("Nothing is playing.")

@bot.command()
async def resume(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("Nothing is paused.")

@bot.command()
async def stop(ctx):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("I’m not in a voice channel.")

    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await ctx.send("⏹️ Stopped and cleared the queue.")
    log.info("Stopped playback guild=%s", ctx.guild.id)


# ---------- START ----------
bot.run(TOKEN)
