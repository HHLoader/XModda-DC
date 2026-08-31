import os
import re
import json
import asyncio
import datetime as dt
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

# ============================================================
# XMODDA — FULL DISCORD BOT
# Database: Supabase REST API
# Runtime: Render / any Python host
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
PORT = int(os.getenv("PORT", "8080"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

# ---------- Health server ----------
health = Flask(__name__)

@health.get("/")
def health_root():
    return "XModda is online"


def run_health():
    health.run(host="0.0.0.0", port=PORT)

# ---------- Constants ----------
DEFAULTS = {
    # AutoMod
    "antiSpam": False,
    "antiLinks": False,
    "antiInvites": False,
    "badWords": False,
    "caps": False,
    "duplicate": False,
    "raid": False,
    "antiSpamLimit": 5,
    "antiSpamWindow": 6,
    "duplicateLimit": 3,
    "capsPercent": 70,
    "capsMinLength": 8,
    "raidJoinLimit": 8,
    "raidWindow": 10,
    "timeoutMinutes": 5,
    "badWordsList": [],
    "ignoredChannels": [],
    "bypassRoles": [],
    # Logging
    "logging": False,
    "logChannelId": None,
    # Welcome
    "welcome": False,
    "welcomeChannelId": None,
    "welcomeMessage": "Welcome {user} to **{server}**! You are member #{count}.",
    # Auto role
    "autoRole": False,
    "autoRoleId": None,
    # Tickets
    "tickets": True,
    "ticketCategoryId": None,
    "ticketStaffRoleId": None,
    "ticketPanelChannelId": None,
    "ticketLimit": 1,
    # Misc
    "prefix": "!",
}

URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|co|me|tv|dev|xyz|info|site|online|app|ly|us|uk|ca)\b",
    re.I,
)
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+",
    re.I,
)

# ---------- Runtime state ----------
settings_cache: dict[int, dict[str, Any]] = {}
settings_cache_at: dict[int, float] = {}
SETTINGS_TTL = 10

spam_history: defaultdict[tuple[int, int], deque] = defaultdict(deque)
duplicate_history: defaultdict[tuple[int, int], deque] = defaultdict(deque)
violations: defaultdict[tuple[int, int], deque] = defaultdict(deque)
join_history: defaultdict[int, deque] = defaultdict(deque)

# ---------- Supabase ----------
def db_headers() -> dict[str, str]:
    # sb_secret_ keys are API keys, not JWTs. apikey is the correct header.
    return {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def db_get_sync(guild_id: int) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing.")
    q = urlencode({"guild_id": f"eq.{guild_id}", "select": "settings", "limit": "1"})
    req = Request(f"{SUPABASE_URL}/rest/v1/guild_settings?{q}", headers=db_headers(), method="GET")
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data[0].get("settings") or {} if data else {}


def db_upsert_sync(guild_id: int, settings: dict[str, Any]) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing.")
    req = Request(
        f"{SUPABASE_URL}/rest/v1/guild_settings",
        headers={**db_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        data=json.dumps({
            "guild_id": str(guild_id),
            "settings": settings,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }).encode(),
        method="POST",
    )
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data[0].get("settings", settings) if data else settings


async def get_settings(guild_id: int, force: bool = False) -> dict[str, Any]:
    now = asyncio.get_running_loop().time()
    if not force and guild_id in settings_cache and now - settings_cache_at.get(guild_id, 0) < SETTINGS_TTL:
        return settings_cache[guild_id]
    try:
        raw = await asyncio.to_thread(db_get_sync, guild_id)
        merged = dict(DEFAULTS)
        if isinstance(raw, dict):
            merged.update(raw)
        settings_cache[guild_id] = merged
        settings_cache_at[guild_id] = now
        return merged
    except Exception as e:
        print(f"[Supabase] GET guild {guild_id} failed: {e}")
        if guild_id in settings_cache:
            return settings_cache[guild_id]
        return dict(DEFAULTS)


async def save_settings(guild_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    current = await get_settings(guild_id)
    current.update(changes)
    saved = await asyncio.to_thread(db_upsert_sync, guild_id, current)
    settings_cache[guild_id] = saved
    settings_cache_at[guild_id] = asyncio.get_running_loop().time()
    return saved

# ---------- Discord ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_staff(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.manage_guild or p.manage_messages


def bypassed(member: discord.Member, settings: dict[str, Any]) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    bypass_roles = {int(x) for x in settings.get("bypassRoles", []) if str(x).isdigit()}
    return any(r.id in bypass_roles for r in member.roles)


def ignored(message: discord.Message, settings: dict[str, Any]) -> bool:
    return message.channel.id in {int(x) for x in settings.get("ignoredChannels", []) if str(x).isdigit()}


async def send_log(guild: discord.Guild, title: str, description: str, color: int = 0x5865F2):
    settings = await get_settings(guild.id)
    if not settings.get("logging"):
        return
    cid = settings.get("logChannelId")
    if not cid:
        return
    channel = guild.get_channel(int(cid))
    if not channel:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=dt.datetime.now(dt.timezone.utc))
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def violation(message: discord.Message, reason: str, settings: dict[str, Any]):
    guild = message.guild
    member = message.author
    try:
        await message.delete()
    except discord.HTTPException as e:
        print(f"[AutoMod] Could not delete message: {e}")

    key = (guild.id, member.id)
    now = asyncio.get_running_loop().time()
    h = violations[key]
    h.append(now)
    while h and now - h[0] > 600:
        h.popleft()

    try:
        await message.channel.send(
            f"⚠️ {member.mention}, your message was removed: **{reason}**",
            delete_after=6,
        )
    except discord.HTTPException:
        pass

    timeout_minutes = max(0, int(settings.get("timeoutMinutes", 5) or 0))
    if len(h) >= 3 and timeout_minutes and guild.me and guild.me.guild_permissions.moderate_members:
        try:
            await member.timeout(dt.timedelta(minutes=timeout_minutes), reason=f"XModda AutoMod: {reason}")
        except discord.HTTPException:
            pass

    await send_log(
        guild,
        "AutoMod action",
        f"**User:** {member.mention}\n**Channel:** {message.channel.mention}\n**Reason:** {reason}",
        0xED4245,
    )


async def automod(message: discord.Message) -> bool:
    if not message.guild or message.author.bot or not message.content:
        return False
    settings = await get_settings(message.guild.id)
    if bypassed(message.author, settings) or ignored(message, settings):
        return False

    content = message.content
    now = asyncio.get_running_loop().time()
    key = (message.guild.id, message.author.id)

    # Invite detection comes before generic links.
    if settings.get("antiInvites") and INVITE_RE.search(content):
        await violation(message, "Discord invites are disabled in this server.", settings)
        return True

    if settings.get("antiLinks") and URL_RE.search(content):
        await violation(message, "Links are disabled in this server.", settings)
        return True

    words = settings.get("badWordsList") or []
    if settings.get("badWords") and any(str(w).strip().casefold() in content.casefold() for w in words if str(w).strip()):
        await violation(message, "A blocked word or phrase was detected.", settings)
        return True

    if settings.get("caps"):
        letters = [c for c in content if c.isalpha()]
        minimum = max(1, int(settings.get("capsMinLength", 8) or 8))
        percent = (sum(c.isupper() for c in letters) / len(letters) * 100) if letters else 0
        if len(letters) >= minimum and percent >= float(settings.get("capsPercent", 70) or 70):
            await violation(message, "Excessive uppercase text is not allowed.", settings)
            return True

    if settings.get("duplicate"):
        d = duplicate_history[key]
        normalized = re.sub(r"\s+", " ", content.strip().casefold())
        d.append((now, normalized))
        while d and now - d[0][0] > 20:
            d.popleft()
        count = sum(1 for _, text in d if text == normalized)
        if count >= max(2, int(settings.get("duplicateLimit", 3) or 3)):
            await violation(message, "Repeated duplicate messages are not allowed.", settings)
            return True

    if settings.get("antiSpam"):
        s = spam_history[key]
        s.append(now)
        window = max(1, int(settings.get("antiSpamWindow", 6) or 6))
        limit = max(2, int(settings.get("antiSpamLimit", 5) or 5))
        while s and now - s[0] > window:
            s.popleft()
        if len(s) > limit:
            await violation(message, f"Too many messages in {window} seconds.", settings)
            return True

    return False

# ---------- Events ----------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"[XModda] Logged in as {bot.user} | synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[XModda] Slash sync failed: {e}")
    print(f"[XModda] Connected to {len(bot.guilds)} guild(s)")
    print(f"[XModda] Supabase configured: {bool(SUPABASE_URL and SUPABASE_KEY)}")


@bot.event
async def on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        await automod(message)
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    settings = await get_settings(member.guild.id)

    # Welcome
    if settings.get("welcome") and settings.get("welcomeChannelId"):
        channel = member.guild.get_channel(int(settings["welcomeChannelId"]))
        if channel:
            text = str(settings.get("welcomeMessage") or DEFAULTS["welcomeMessage"])
            text = text.replace("{user}", member.mention).replace("{username}", member.name)
            text = text.replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count or 0))
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    # Auto role
    if settings.get("autoRole") and settings.get("autoRoleId"):
        role = member.guild.get_role(int(settings["autoRoleId"]))
        if role and member.guild.me and role < member.guild.me.top_role:
            try:
                await member.add_roles(role, reason="XModda auto-role")
            except discord.HTTPException:
                pass

    # Raid detection
    if settings.get("raid"):
        now = asyncio.get_running_loop().time()
        joins = join_history[member.guild.id]
        joins.append(now)
        window = max(1, int(settings.get("raidWindow", 10) or 10))
        limit = max(2, int(settings.get("raidJoinLimit", 8) or 8))
        while joins and now - joins[0] > window:
            joins.popleft()
        if len(joins) >= limit:
            await send_log(member.guild, "🚨 Possible raid detected", f"{len(joins)} members joined within {window} seconds.", 0xED4245)

# ---------- Error helper ----------
def ok_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=0x57F287)


def err_embed(text: str) -> discord.Embed:
    return discord.Embed(title="XModda", description=f"❌ {text}", color=0xED4245)

# ---------- General ----------
@bot.tree.command(name="ping", description="Check XModda latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency * 1000)}ms`", ephemeral=True)


@bot.tree.command(name="serverinfo", description="Show server information")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    e = discord.Embed(title=g.name, color=0x5865F2)
    e.add_field(name="Members", value=str(g.member_count))
    e.add_field(name="Channels", value=str(len(g.channels)))
    e.add_field(name="Owner", value=f"<@{g.owner_id}>")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="userinfo", description="Show information about a member")
@app_commands.describe(member="Member to inspect")
async def userinfo(interaction: discord.Interaction, member: discord.Member):
    e = discord.Embed(title=f"User Info — {member}", color=member.color.value or 0x5865F2)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=str(member.id), inline=False)
    e.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="avatar", description="Show a member's avatar")
@app_commands.describe(member="Member")
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    e = discord.Embed(title=f"{member.display_name}'s Avatar", color=0x5865F2)
    e.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=e)

# ---------- Moderation ----------
@bot.tree.command(name="purge", description="Delete messages")
@app_commands.describe(amount="Number of messages to delete (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.response.send_message(f"🗑️ Deleted {len(deleted)} messages.", ephemeral=True)
    await send_log(interaction.guild, "Messages purged", f"{interaction.user.mention} deleted {len(deleted)} messages in {interaction.channel.mention}")


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member", reason="Reason")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(embed=err_embed("You cannot kick this member."), ephemeral=True)
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 Kicked **{member}**.")
    await send_log(interaction.guild, "Member kicked", f"**Member:** {member}\n**By:** {interaction.user.mention}\n**Reason:** {reason}", 0xED4245)


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member", reason="Reason")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(embed=err_embed("You cannot ban this member."), ephemeral=True)
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 Banned **{member}**.")
    await send_log(interaction.guild, "Member banned", f"**Member:** {member}\n**By:** {interaction.user.mention}\n**Reason:** {reason}", 0xED4245)


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(member="Member", minutes="Duration in minutes", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
    await interaction.response.send_message(f"⏱️ Timed out **{member}** for `{minutes}` minutes.")
    await send_log(interaction.guild, "Member timed out", f"**Member:** {member}\n**By:** {interaction.user.mention}\n**Duration:** {minutes}m\n**Reason:** {reason}", 0xED4245)


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.describe(member="Member", reason="Reason")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    key = f"warnings_{interaction.guild.id}"
    # Keep warning records inside the same JSON settings row.
    settings = await get_settings(interaction.guild.id)
    warning_map = settings.get(key, {})
    uid = str(member.id)
    warning_map[uid] = int(warning_map.get(uid, 0)) + 1
    await save_settings(interaction.guild.id, {key: warning_map})
    await interaction.response.send_message(f"⚠️ Warned **{member}**. Warnings: `{warning_map[uid]}`")
    await send_log(interaction.guild, "Member warned", f"**Member:** {member}\n**By:** {interaction.user.mention}\n**Reason:** {reason}", 0xFEE75C)


@bot.tree.command(name="warnings", description="View a member's warning count")
@app_commands.describe(member="Member")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    settings = await get_settings(interaction.guild.id)
    warning_map = settings.get(f"warnings_{interaction.guild.id}", {})
    await interaction.response.send_message(f"⚠️ **{member}** has `{warning_map.get(str(member.id), 0)}` warning(s).", ephemeral=True)

# ---------- AutoMod commands ----------
@bot.tree.command(name="automod_status", description="Show the live AutoMod settings XModda is using")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_status(interaction: discord.Interaction):
    s = await get_settings(interaction.guild.id, force=True)
    e = discord.Embed(title="XModda AutoMod Status", color=0x5865F2)
    for key, label in [
        ("antiLinks", "Anti-Links"), ("antiInvites", "Anti-Invites"), ("antiSpam", "Anti-Spam"),
        ("duplicate", "Duplicate"), ("caps", "Excessive Caps"), ("badWords", "Bad Words"), ("raid", "Raid Protection")
    ]:
        e.add_field(name=label, value="🟢 ON" if s.get(key) else "🔴 OFF", inline=True)
    e.add_field(name="Spam", value=f"{s['antiSpamLimit']} msgs / {s['antiSpamWindow']}s", inline=True)
    e.add_field(name="Duplicate", value=f"{s['duplicateLimit']} repeats", inline=True)
    e.add_field(name="Caps", value=f"{s['capsPercent']}% / {s['capsMinLength']} letters", inline=True)
    e.add_field(name="Timeout", value=f"{s['timeoutMinutes']} minutes after repeated violations", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="automod_reload", description="Immediately reload this server's dashboard settings")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_reload(interaction: discord.Interaction):
    s = await get_settings(interaction.guild.id, force=True)
    await interaction.response.send_message(
        f"🔄 Settings reloaded. Anti-Links: **{'ON' if s.get('antiLinks') else 'OFF'}**, "
        f"Anti-Spam: **{'ON' if s.get('antiSpam') else 'OFF'}**.", ephemeral=True
    )

# ---------- Settings commands ----------
@bot.tree.command(name="welcome_config", description="Configure welcome messages")
@app_commands.describe(channel="Welcome channel", message="Message; use {user}, {username}, {server}, {count}")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_config(interaction: discord.Interaction, channel: discord.TextChannel, message: str = DEFAULTS["welcomeMessage"]):
    await save_settings(interaction.guild.id, {"welcome": True, "welcomeChannelId": channel.id, "welcomeMessage": message})
    await interaction.response.send_message(embed=ok_embed("Welcome enabled", f"Channel: {channel.mention}\nMessage: {message}"), ephemeral=True)


@bot.tree.command(name="welcome_disable", description="Disable welcome messages")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_disable(interaction: discord.Interaction):
    await save_settings(interaction.guild.id, {"welcome": False})
    await interaction.response.send_message("✅ Welcome messages disabled.", ephemeral=True)


@bot.tree.command(name="autorole", description="Set the role automatically given to new members")
@app_commands.describe(role="Role to give")
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild.me and role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(embed=err_embed("That role must be below XModda's highest role."), ephemeral=True)
    await save_settings(interaction.guild.id, {"autoRole": True, "autoRoleId": role.id})
    await interaction.response.send_message(f"✅ Auto-role enabled: {role.mention}", ephemeral=True)


@bot.tree.command(name="autorole_disable", description="Disable automatic roles")
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_disable(interaction: discord.Interaction):
    await save_settings(interaction.guild.id, {"autoRole": False})
    await interaction.response.send_message("✅ Auto-role disabled.", ephemeral=True)


@bot.tree.command(name="logging_config", description="Set the moderation log channel")
@app_commands.describe(channel="Log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_config(interaction: discord.Interaction, channel: discord.TextChannel):
    await save_settings(interaction.guild.id, {"logging": True, "logChannelId": channel.id})
    await interaction.response.send_message(f"✅ Logging enabled in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="logging_disable", description="Disable logging")
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_disable(interaction: discord.Interaction):
    await save_settings(interaction.guild.id, {"logging": False})
    await interaction.response.send_message("✅ Logging disabled.", ephemeral=True)


@bot.tree.command(name="automod_word", description="Add a word/phrase to the blocked-word list")
@app_commands.describe(word="Word or phrase")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word(interaction: discord.Interaction, word: str):
    s = await get_settings(interaction.guild.id)
    words = [str(x) for x in (s.get("badWordsList") or [])]
    if word.casefold() not in [x.casefold() for x in words]:
        words.append(word)
    await save_settings(interaction.guild.id, {"badWords": True, "badWordsList": words})
    await interaction.response.send_message(f"✅ Added `{word}` to the blocked-word list.", ephemeral=True)


@bot.tree.command(name="automod_word_remove", description="Remove a word/phrase from the blocked-word list")
@app_commands.describe(word="Word or phrase")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word_remove(interaction: discord.Interaction, word: str):
    s = await get_settings(interaction.guild.id)
    words = [x for x in (s.get("badWordsList") or []) if str(x).casefold() != word.casefold()]
    await save_settings(interaction.guild.id, {"badWordsList": words})
    await interaction.response.send_message(f"✅ Removed `{word}` if it existed.", ephemeral=True)

# ---------- Tickets ----------
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="xmodda:ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        s = await get_settings(guild.id)
        category = guild.get_channel(int(s["ticketCategoryId"])) if s.get("ticketCategoryId") else None
        staff_role = guild.get_role(int(s["ticketStaffRoleId"])) if s.get("ticketStaffRoleId") else None
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Tickets are not configured. An admin must set the category first.", ephemeral=True)

        existing = [c for c in category.channels if isinstance(c, discord.TextChannel) and c.topic == f"xmodda-ticket:{interaction.user.id}"]
        if existing:
            return await interaction.response.send_message(f"You already have a ticket: {existing[0].mention}", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}"[:95],
            category=category,
            overwrites=overwrites,
            topic=f"xmodda-ticket:{interaction.user.id}",
            reason="XModda ticket opened",
        )
        view = CloseTicketView()
        await channel.send(
            f"🎫 Welcome {interaction.user.mention}!\nPlease explain your issue. "
            f"{staff_role.mention if staff_role else ''}",
            view=view,
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        await send_log(guild, "Ticket opened", f"{interaction.user.mention} opened {channel.mention}")


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="xmodda:ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (isinstance(interaction.user, discord.Member) and (is_staff(interaction.user) or interaction.channel.topic == f"xmodda-ticket:{interaction.user.id}")):
            return await interaction.response.send_message("❌ You cannot close this ticket.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing ticket...", ephemeral=True)
        await send_log(interaction.guild, "Ticket closed", f"{interaction.channel.mention} closed by {interaction.user.mention}")
        await asyncio.sleep(1)
        await interaction.channel.delete(reason="XModda ticket closed")


@bot.tree.command(name="ticket_config", description="Configure the ticket system")
@app_commands.describe(category="Ticket category", staff_role="Staff role")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_config(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
    await save_settings(interaction.guild.id, {
        "tickets": True,
        "ticketCategoryId": category.id,
        "ticketStaffRoleId": staff_role.id,
    })
    await interaction.response.send_message(f"✅ Tickets configured. Category: {category.name} | Staff: {staff_role.mention}", ephemeral=True)


@bot.tree.command(name="ticket_panel", description="Post the ticket panel in the current channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_panel(interaction: discord.Interaction):
    await interaction.response.send_message("🎫 **Support Tickets**\nClick below to open a private support ticket.", view=TicketView())

# ---------- Error handler ----------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You do not have the required Discord permission for this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = "That command is on cooldown."
    else:
        print(f"[Command Error] {repr(error)}")
        msg = "Something went wrong while running that command. Check the bot logs."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

# ---------- Main ----------
def main():
    import threading
    threading.Thread(target=run_health, daemon=True).start()
    bot.add_view(TicketView())
    bot.add_view(CloseTicketView())
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
