import asyncio
import datetime as dt
import html
import json
import os
import re
import threading
from collections import defaultdict, deque
from copy import deepcopy

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask

# ============================================================
# XModda Discord Bot
# - Per-server configuration
# - Moderation / warnings
# - Tickets
# - AutoMod: links, invites, spam, caps, word filter
# - Logging
#
# Environment variable required:
#   DISCORD_TOKEN
#
# IMPORTANT:
# Enable "Message Content Intent" and "Server Members Intent"
# in Discord Developer Portal -> Bot -> Privileged Gateway Intents.
# ============================================================

# -----------------------------
# Keep Render/UptimeRobot alive
# -----------------------------
web_app = Flask(__name__)


@web_app.get("/")
def health():
    return "XModda is alive", 200


def run_web_server():
    port = int(os.environ.get("PORT", "8080"))
    web_app.run(host="0.0.0.0", port=port)


# -----------------------------
# Discord setup
# -----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_FILE = "config.json"
WARNINGS_FILE = "warnings.json"

DEFAULT_CONFIG = {
    "automod": {
        "anti_links": {
            "enabled": False,
            "block_all_links": True,
            "allowed_domains": [],
            "ignored_channels": [],
            "ignored_roles": [],
            "delete_message": True,
            "warn_user": False,
            "log": True,
            "timeout_after": 0,
            "timeout_minutes": 10,
        },
        "anti_invites": {
            "enabled": False,
            "delete_message": True,
            "warn_user": True,
            "log": True,
            "timeout_after": 0,
            "timeout_minutes": 10,
        },
        "anti_spam": {
            "enabled": False,
            "message_limit": 5,
            "window_seconds": 5,
            "delete_messages": True,
            "warn_user": True,
            "log": True,
            "timeout_after": 3,
            "timeout_minutes": 10,
            "duplicate_limit": 3,
        },
        "anti_caps": {
            "enabled": False,
            "minimum_length": 10,
            "caps_percentage": 70,
            "delete_message": True,
            "warn_user": True,
            "log": True,
        },
        "word_filter": {
            "enabled": False,
            "blocked_words": [],
            "delete_message": True,
            "warn_user": True,
            "log": True,
        },
        "bypass_roles": [],
        "ignored_channels": [],
    },
    "tickets": {
        "ticket_category_id": None,
        "staff_role_id": None,
        "log_channel_id": None,
        "panel_embed_title": "Support Tickets",
        "panel_embed_description": "Click the button below to open a support ticket.",
        "panel_embed_color": 0x00FF00,
        "panel_embed_thumbnail": None,
        "opened_embed_title": "Ticket Created",
        "opened_embed_description": (
            "Welcome {user}! Please describe your issue. "
            "Staff will assist shortly.\n\nUse the buttons below to manage this ticket."
        ),
        "opened_embed_color": 0x0000FF,
        "opened_embed_thumbnail": None,
        "ticket_limit": 1,
    },
}


config = {}
warnings = {}

# -----------------------------
# Runtime AutoMod state
# -----------------------------
# (guild_id, user_id) -> timestamps
spam_messages = defaultdict(deque)
# (guild_id, user_id) -> timestamps of recent violations
violation_history = defaultdict(lambda: defaultdict(deque))


def deep_copy_default():
    return deepcopy(DEFAULT_CONFIG)


def deep_merge(base, incoming):
    """Merge old config files with newer defaults without losing old settings."""
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        return deepcopy(incoming)
    result = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return deepcopy(fallback)


def save_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    global config
    raw = load_json(CONFIG_FILE, {})
    config = raw if isinstance(raw, dict) else {}


def save_config():
    save_json(CONFIG_FILE, config)


def load_warnings():
    global warnings
    raw = load_json(WARNINGS_FILE, {})
    warnings = raw if isinstance(raw, dict) else {}


def save_warnings():
    save_json(WARNINGS_FILE, warnings)


def get_guild_config(guild_id):
    gid = str(guild_id)
    if gid not in config or not isinstance(config[gid], dict):
        config[gid] = deep_copy_default()
        save_config()
    else:
        merged = deep_merge(deep_copy_default(), config[gid])
        if merged != config[gid]:
            config[gid] = merged
            save_config()
    return config[gid]


def get_ticket_config(guild_id):
    return get_guild_config(guild_id)["tickets"]


def get_automod_config(guild_id):
    return get_guild_config(guild_id)["automod"]


def get_warning_list(guild_id, user_id):
    guild_key = str(guild_id)
    user_key = str(user_id)
    guild_warnings = warnings.setdefault(guild_key, {})
    return guild_warnings.setdefault(user_key, [])


# -----------------------------
# Helpers
# -----------------------------
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>()]+|"
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|co|me|tv|dev|app|xyz|site|online|"
    r"store|info|biz|us|uk|ca|de|fr|ly|link)(?:/[^\s<>()]*)?"
)

DISCORD_INVITE_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/[A-Za-z0-9-]+"
)


def normalize_domain(value):
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def message_domains(content):
    domains = []
    for match in URL_RE.findall(content or ""):
        raw = match.lower().rstrip(".,!?)]}>")
        raw = re.sub(r"^https?://", "", raw)
        raw = re.sub(r"^www\.", "", raw)
        domains.append(raw.split("/", 1)[0])
    return domains


def is_allowed_domain(domain, allowed):
    domain = normalize_domain(domain)
    for item in allowed:
        item = normalize_domain(item)
        if not item:
            continue
        if domain == item or domain.endswith("." + item):
            return True
    return False


def member_bypasses_automod(member, guild_cfg):
    if not isinstance(member, discord.Member):
        return True
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    role_ids = {str(r.id) for r in member.roles}
    bypass = {str(rid) for rid in guild_cfg["automod"].get("bypass_roles", [])}
    return bool(role_ids & bypass)


def channel_ignored(channel, guild_cfg, feature_cfg=None):
    ignored = {str(x) for x in guild_cfg["automod"].get("ignored_channels", [])}
    if feature_cfg:
        ignored |= {str(x) for x in feature_cfg.get("ignored_channels", [])}
    return str(channel.id) in ignored


def role_ignored(member, guild_cfg, feature_cfg=None):
    role_ids = {str(r.id) for r in member.roles}
    ignored = {str(x) for x in guild_cfg["automod"].get("bypass_roles", [])}
    if feature_cfg:
        ignored |= {str(x) for x in feature_cfg.get("ignored_roles", [])}
    return bool(role_ids & ignored)


def can_bot_moderate(member):
    me = member.guild.me
    if me is None:
        return False
    if member.id == me.id:
        return False
    return member.top_role < me.top_role


async def send_dm_embed(member, action, reason, guild_name):
    embed = discord.Embed(
        title=f"You have been {action}",
        description=f"**Server:** {guild_name}\n**Reason:** {reason}",
        color=0xFF0000,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.set_footer(text="This action was performed by XModda.")
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def log_event(guild, title, description, color=0xF1C40F):
    cfg = get_guild_config(guild.id)
    channel_id = cfg["tickets"].get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(
        title=title,
        description=description[:4000],
        color=color,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def add_warning(guild, user, moderator_id, reason):
    entries = get_warning_list(guild.id, user.id)
    entries.append({
        "reason": reason,
        "warned_by": int(moderator_id),
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    # Prevent an accidental infinite file growth.
    if len(entries) > 100:
        del entries[:-100]
    save_warnings()
    return len(entries)


async def automod_warning(guild, member, reason, feature_cfg):
    if not feature_cfg.get("warn_user", False):
        return len(get_warning_list(guild.id, member.id))

    count = await add_warning(guild, member, bot.user.id if bot.user else 0, reason)

    try:
        await member.send(
            embed=discord.Embed(
                title="XModda AutoMod Warning",
                description=f"**Server:** {guild.name}\n**Reason:** {reason}",
                color=0xFFA500,
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    return count


async def maybe_timeout_after_violation(guild, member, feature_name, feature_cfg):
    timeout_after = int(feature_cfg.get("timeout_after", 0) or 0)
    if timeout_after <= 0:
        return

    key = (guild.id, member.id)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    history = violation_history[key][feature_name]
    history.append(now)

    # Keep only a rolling 24-hour window.
    cutoff = now - 86400
    while history and history[0] < cutoff:
        history.popleft()

    if len(history) < timeout_after:
        return

    minutes = max(1, int(feature_cfg.get("timeout_minutes", 10) or 10))
    if not can_bot_moderate(member):
        return

    try:
        await member.timeout(
            dt.timedelta(minutes=minutes),
            reason=f"XModda AutoMod: {feature_name} escalation",
        )
        history.clear()
        await log_event(
            guild,
            "AutoMod Timeout",
            f"{member.mention} was timed out for **{minutes} minutes** "
            f"after repeated **{feature_name}** violations.",
            0xE74C3C,
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


async def handle_violation(message, feature_name, feature_cfg, reason):
    guild = message.guild
    member = message.author

    if feature_cfg.get("delete_message", feature_cfg.get("delete_messages", True)):
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    count = await automod_warning(guild, member, reason, feature_cfg)

    if feature_cfg.get("log", True):
        await log_event(
            guild,
            f"AutoMod: {feature_name}",
            f"User: {member.mention} (`{member.id}`)\n"
            f"Channel: {message.channel.mention}\n"
            f"Reason: {reason}\n"
            f"Warnings: {count}\n"
            f"Message: {message.content[:1000] or '[no text]'}",
            0xE67E22,
        )

    await maybe_timeout_after_violation(guild, member, feature_name, feature_cfg)
    return True


def is_caps_violation(content, minimum_length, percentage):
    letters = [c for c in content if c.isalpha()]
    if len(letters) < minimum_length:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) * 100 >= percentage


async def run_automod(message):
    if not message.guild or message.author.bot:
        return False

    cfg = get_guild_config(message.guild.id)
    member = message.author

    if member_bypasses_automod(member, cfg):
        return False

    automod = cfg["automod"]

    # Global ignored channel / feature-specific ignored channel.
    if str(message.channel.id) in {str(x) for x in automod.get("ignored_channels", [])}:
        return False

    # Anti-Discord-Invites is checked before general links.
    invite_cfg = automod["anti_invites"]
    if invite_cfg.get("enabled") and not channel_ignored(message.channel, cfg, invite_cfg):
        if not role_ignored(member, cfg, invite_cfg) and DISCORD_INVITE_RE.search(message.content or ""):
            return await handle_violation(
                message,
                "Anti-Discord-Invites",
                invite_cfg,
                "Discord server invite links are not allowed.",
            )

    link_cfg = automod["anti_links"]
    if link_cfg.get("enabled") and not channel_ignored(message.channel, cfg, link_cfg):
        if not role_ignored(member, cfg, link_cfg):
            domains = message_domains(message.content)
            if domains:
                if link_cfg.get("block_all_links", True):
                    allowed = all(
                        is_allowed_domain(d, link_cfg.get("allowed_domains", []))
                        for d in domains
                    )
                    if not allowed:
                        return await handle_violation(
                            message,
                            "Anti-Links",
                            link_cfg,
                            "Links are not allowed in this channel.",
                        )
                else:
                    not_allowed = [
                        d for d in domains
                        if not is_allowed_domain(d, link_cfg.get("allowed_domains", []))
                    ]
                    if not_allowed:
                        return await handle_violation(
                            message,
                            "Anti-Links",
                            link_cfg,
                            "This domain is not on the allowed-domain list: "
                            + ", ".join(not_allowed[:5]),
                        )

    caps_cfg = automod["anti_caps"]
    if caps_cfg.get("enabled") and not channel_ignored(message.channel, cfg, caps_cfg):
        if not role_ignored(member, cfg, caps_cfg):
            if is_caps_violation(
                message.content or "",
                int(caps_cfg.get("minimum_length", 10)),
                int(caps_cfg.get("caps_percentage", 70)),
            ):
                return await handle_violation(
                    message,
                    "Anti-Caps",
                    caps_cfg,
                    f"Too much uppercase text "
                    f"(limit {caps_cfg.get('caps_percentage', 70)}%).",
                )

    word_cfg = automod["word_filter"]
    if word_cfg.get("enabled") and not channel_ignored(message.channel, cfg, word_cfg):
        if not role_ignored(member, cfg, word_cfg):
            content_lower = (message.content or "").casefold()
            blocked = [
                str(w).strip()
                for w in word_cfg.get("blocked_words", [])
                if str(w).strip()
            ]
            found = next(
                (w for w in blocked if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", content_lower)),
                None,
            )
            if found:
                return await handle_violation(
                    message,
                    "Word Filter",
                    word_cfg,
                    f"Blocked word detected: `{found}`",
                )

    spam_cfg = automod["anti_spam"]
    if spam_cfg.get("enabled") and not channel_ignored(message.channel, cfg, spam_cfg):
        if not role_ignored(member, cfg, spam_cfg):
            now = dt.datetime.now(dt.timezone.utc).timestamp()
            key = (message.guild.id, member.id)
            queue = spam_messages[key]
            window = max(1, int(spam_cfg.get("window_seconds", 5)))
            limit = max(2, int(spam_cfg.get("message_limit", 5)))

            while queue and queue[0] <= now - window:
                queue.popleft()
            queue.append(now)

            if len(queue) >= limit:
                queue.clear()
                return await handle_violation(
                    message,
                    "Anti-Spam",
                    spam_cfg,
                    f"Too many messages in {window} seconds "
                    f"(limit {limit}).",
                )

            # Duplicate-message protection.
            duplicate_limit = max(0, int(spam_cfg.get("duplicate_limit", 3)))
            if duplicate_limit > 1 and message.content.strip():
                recent_messages = getattr(message.guild, "_xmodda_recent_messages", None)
                if recent_messages is None:
                    recent_messages = defaultdict(lambda: deque(maxlen=10))
                    setattr(message.guild, "_xmodda_recent_messages", recent_messages)

                recent = recent_messages[member.id]
                recent.append(message.content.casefold().strip())
                if len(recent) >= duplicate_limit:
                    last = list(recent)[-duplicate_limit:]
                    if len(set(last)) == 1:
                        recent.clear()
                        return await handle_violation(
                            message,
                            "Anti-Spam",
                            spam_cfg,
                            f"Repeated the same message {duplicate_limit} times.",
                        )

    return False


# ============================================================
# Tickets
# ============================================================

class CreateTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Ticket",
        style=discord.ButtonStyle.green,
        custom_id="xmodda:create_ticket",
    )
    async def create_ticket_button(self, interaction, button):
        await create_ticket(interaction)


class TicketManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.primary,
        custom_id="xmodda:claim_ticket",
    )
    async def claim_ticket_button(self, interaction, button):
        await claim_ticket(interaction)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        custom_id="xmodda:close_ticket",
    )
    async def close_ticket_button(self, interaction, button):
        await close_ticket(interaction)


async def create_ticket(interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "This can only be used inside a server.", ephemeral=True
        )

    cfg = get_ticket_config(guild.id)
    member = interaction.user

    category_id = cfg.get("ticket_category_id")
    staff_role_id = cfg.get("staff_role_id")

    if not category_id or not staff_role_id:
        return await interaction.response.send_message(
            "Ticket category or staff role is not configured. "
            "An administrator must use `/ticket_config` first.",
            ephemeral=True,
        )

    category = guild.get_channel(int(category_id))
    staff_role = guild.get_role(int(staff_role_id))

    if not isinstance(category, discord.CategoryChannel) or staff_role is None:
        return await interaction.response.send_message(
            "The configured ticket category or staff role no longer exists.",
            ephemeral=True,
        )

    limit = max(1, min(10, int(cfg.get("ticket_limit", 1))))
    prefix = f"ticket-{member.name.lower()}".replace(" ", "-")
    open_tickets = [
        ch for ch in guild.text_channels
        if ch.name.startswith(prefix) and ch.category_id == category.id
    ]

    if len(open_tickets) >= limit:
        return await interaction.response.send_message(
            f"You already have {limit} open ticket(s).",
            ephemeral=True,
        )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        staff_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True
        ),
    }

    try:
        ticket_channel = await guild.create_text_channel(
            name=prefix,
            category=category,
            overwrites=overwrites,
            reason=f"XModda ticket opened by {member}",
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(
            f"Could not create the ticket: `{e}`", ephemeral=True
        )

    embed = discord.Embed(
        title=cfg.get("opened_embed_title", "Ticket Created"),
        description=cfg.get("opened_embed_description", "Welcome {user}!").replace(
            "{user}", member.mention
        ),
        color=int(cfg.get("opened_embed_color", 0x0000FF)),
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")

    thumbnail = cfg.get("opened_embed_thumbnail")
    if thumbnail:
        try:
            embed.set_thumbnail(url=thumbnail)
        except Exception:
            pass

    await ticket_channel.send(embed=embed, view=TicketManageView())

    await log_event(
        guild,
        "Ticket Created",
        f"{ticket_channel.mention} created by {member.mention}.",
        0x2ECC71,
    )

    await interaction.response.send_message(
        f"Ticket created: {ticket_channel.mention}", ephemeral=True
    )


async def claim_ticket(interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "This can only be used inside a server.", ephemeral=True
        )

    cfg = get_ticket_config(guild.id)
    staff_role_id = cfg.get("staff_role_id")

    if not staff_role_id:
        return await interaction.response.send_message(
            "Staff role is not configured.", ephemeral=True
        )

    staff_role = guild.get_role(int(staff_role_id))
    if staff_role is None or staff_role not in interaction.user.roles:
        return await interaction.response.send_message(
            "You are not staff.", ephemeral=True
        )

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not channel.name.startswith("ticket-"):
        return await interaction.response.send_message(
            "This is not a ticket channel.", ephemeral=True
        )

    try:
        await channel.set_permissions(
            interaction.user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(
            f"Could not claim the ticket: `{e}`", ephemeral=True
        )

    await interaction.response.send_message(
        f"{interaction.user.mention} has claimed this ticket."
    )


async def close_ticket(interaction):
    guild = interaction.guild
    channel = interaction.channel

    if guild is None or not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message(
            "This can only be used in a ticket channel.", ephemeral=True
        )

    cfg = get_ticket_config(guild.id)
    staff_role_id = cfg.get("staff_role_id")
    is_staff = (
        interaction.user.guild_permissions.administrator
        or (
            staff_role_id
            and guild.get_role(int(staff_role_id))
            and guild.get_role(int(staff_role_id)) in interaction.user.roles
        )
    )

    if not is_staff:
        return await interaction.response.send_message(
            "Only staff can close tickets.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    try:
        await save_transcript(channel, guild)
    except Exception as e:
        await log_event(guild, "Transcript Error", f"`{e}`", 0xE74C3C)

    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException):
        try:
            await interaction.followup.send(
                "I couldn't delete this ticket. Check my Manage Channels permission.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


async def save_transcript(channel, guild):
    lines = []
    async for message in channel.history(limit=1000, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        author = str(message.author)
        content = message.content or ""
        if message.attachments:
            content += " " + " ".join(a.url for a in message.attachments)
        if message.embeds and not content:
            content = "[Embed]"
        lines.append(f"[{timestamp}] {author}: {content}")

    body = html.escape("\n".join(lines))
    html_content = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Ticket Transcript</title></head>"
        "<body><h1>Ticket Transcript</h1><pre>"
        + body +
        "</pre></body></html>"
    )

    filename = f"transcript-{channel.id}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    log_channel_id = get_ticket_config(guild.id).get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(int(log_channel_id))
        if isinstance(log_channel, discord.TextChannel):
            try:
                await log_channel.send(
                    f"Transcript for `{channel.name}`:",
                    file=discord.File(filename),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    try:
        os.remove(filename)
    except OSError:
        pass


# ============================================================
# Ticket configuration UI
# ============================================================

class ConfigMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ConfigMainSelect())


class ConfigMainSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="View Current Config", value="view", emoji="📋"),
            discord.SelectOption(label="Set Category", value="category", emoji="📁"),
            discord.SelectOption(label="Set Staff Role", value="role", emoji="👥"),
            discord.SelectOption(label="Set Log Channel", value="log", emoji="📜"),
            discord.SelectOption(label="Set Ticket Limit", value="limit", emoji="🔢"),
            discord.SelectOption(label="Edit Panel Embed", value="panel", emoji="🎨"),
            discord.SelectOption(label="Edit Opened Embed", value="opened", emoji="🎫"),
            discord.SelectOption(label="Post Ticket Panel", value="post", emoji="📬"),
        ]
        super().__init__(
            placeholder="Choose an option...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        cfg = get_ticket_config(interaction.guild.id)
        selected = self.values[0]

        if selected == "view":
            embed = discord.Embed(
                title="Current Ticket Configuration",
                color=0x2ECC71,
            )
            embed.add_field(
                name="Category",
                value=f"<#{cfg['ticket_category_id']}>"
                if cfg.get("ticket_category_id") else "Not set",
                inline=False,
            )
            embed.add_field(
                name="Staff Role",
                value=f"<@&{cfg['staff_role_id']}>"
                if cfg.get("staff_role_id") else "Not set",
                inline=False,
            )
            embed.add_field(
                name="Log Channel",
                value=f"<#{cfg['log_channel_id']}>"
                if cfg.get("log_channel_id") else "Not set",
                inline=False,
            )
            embed.add_field(
                name="Ticket Limit",
                value=str(cfg.get("ticket_limit", 1)),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if selected == "category":
            categories = interaction.guild.categories[:25]
            if not categories:
                return await interaction.response.send_message(
                    "No categories found.", ephemeral=True
                )

            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(
                placeholder="Select a category...",
                options=[
                    discord.SelectOption(label=c.name[:100], value=str(c.id))
                    for c in categories
                ],
            )

            async def callback(i):
                cfg["ticket_category_id"] = int(select.values[0])
                save_config()
                await i.response.send_message("Ticket category updated.", ephemeral=True)

            select.callback = callback
            view.add_item(select)
            await interaction.response.send_message(
                "Select a category:", view=view, ephemeral=True
            )
            return

        if selected == "role":
            roles = [r for r in interaction.guild.roles if not r.is_default()][::-1][:25]
            if not roles:
                return await interaction.response.send_message(
                    "No roles available.", ephemeral=True
                )

            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(
                placeholder="Select the staff role...",
                options=[
                    discord.SelectOption(label=r.name[:100], value=str(r.id))
                    for r in roles
                ],
            )

            async def callback(i):
                cfg["staff_role_id"] = int(select.values[0])
                save_config()
                await i.response.send_message("Staff role updated.", ephemeral=True)

            select.callback = callback
            view.add_item(select)
            await interaction.response.send_message(
                "Select a staff role:", view=view, ephemeral=True
            )
            return

        if selected == "log":
            channels = [
                c for c in interaction.guild.text_channels
                if c.permissions_for(interaction.guild.me).send_messages
            ][:25]
            if not channels:
                return await interaction.response.send_message(
                    "No usable text channels found.", ephemeral=True
                )

            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(
                placeholder="Select the log channel...",
                options=[
                    discord.SelectOption(label=c.name[:100], value=str(c.id))
                    for c in channels
                ],
            )

            async def callback(i):
                cfg["log_channel_id"] = int(select.values[0])
                save_config()
                await i.response.send_message("Log channel updated.", ephemeral=True)

            select.callback = callback
            view.add_item(select)
            await interaction.response.send_message(
                "Select a log channel:", view=view, ephemeral=True
            )
            return

        if selected == "limit":
            await interaction.response.send_modal(
                LimitModal(interaction.guild.id)
            )
            return

        if selected in ("panel", "opened"):
            await interaction.response.send_modal(
                EmbedEditModal(interaction.guild.id, selected)
            )
            return

        if selected == "post":
            embed = discord.Embed(
                title=cfg.get("panel_embed_title", "Support Tickets"),
                description=cfg.get(
                    "panel_embed_description",
                    "Click the button below to open a support ticket.",
                ),
                color=int(cfg.get("panel_embed_color", 0x00FF00)),
            )
            if cfg.get("panel_embed_thumbnail"):
                embed.set_thumbnail(url=cfg["panel_embed_thumbnail"])
            embed.set_footer(text="Powered by XModda")
            await interaction.channel.send(embed=embed, view=CreateTicketView())
            await interaction.response.send_message(
                "Ticket panel posted.", ephemeral=True
            )


class LimitModal(discord.ui.Modal):
    def __init__(self, guild_id):
        super().__init__(title="Set Ticket Limit")
        self.guild_id = guild_id
        self.limit = discord.ui.TextInput(
            label="Max tickets per user",
            placeholder="1-10",
            default=str(get_ticket_config(guild_id).get("ticket_limit", 1)),
            max_length=2,
        )
        self.add_item(self.limit)

    async def on_submit(self, interaction):
        try:
            value = int(self.limit.value)
            if not 1 <= value <= 10:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Enter a number from 1 to 10.", ephemeral=True
            )

        get_ticket_config(self.guild_id)["ticket_limit"] = value
        save_config()
        await interaction.response.send_message(
            f"Ticket limit set to {value}.", ephemeral=True
        )


class EmbedEditModal(discord.ui.Modal):
    def __init__(self, guild_id, embed_type):
        title = "Edit Panel Embed" if embed_type == "panel" else "Edit Opened Ticket Embed"
        super().__init__(title=title)
        self.guild_id = guild_id
        self.embed_type = embed_type
        cfg = get_ticket_config(guild_id)

        prefix = "panel" if embed_type == "panel" else "opened"

        self.title_input = discord.ui.TextInput(
            label="Embed Title",
            default=cfg.get(f"{prefix}_embed_title", ""),
            required=False,
            max_length=256,
        )
        self.desc_input = discord.ui.TextInput(
            label="Embed Description",
            default=cfg.get(f"{prefix}_embed_description", ""),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.color_input = discord.ui.TextInput(
            label="Embed Color (hex)",
            default=f"#{int(cfg.get(f'{prefix}_embed_color', 0)):06X}",
            required=False,
            max_length=7,
        )
        self.thumb_input = discord.ui.TextInput(
            label="Thumbnail URL",
            default=cfg.get(f"{prefix}_embed_thumbnail") or "",
            required=False,
            max_length=500,
        )

        for item in (
            self.title_input,
            self.desc_input,
            self.color_input,
            self.thumb_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        prefix = "panel" if self.embed_type == "panel" else "opened"
        cfg = get_ticket_config(self.guild_id)

        color_text = self.color_input.value.strip()
        try:
            color = int(color_text.lstrip("#"), 16) if color_text else 0x00FF00
            if not 0 <= color <= 0xFFFFFF:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Invalid hex color. Example: `#5865F2`.",
                ephemeral=True,
            )

        cfg[f"{prefix}_embed_title"] = self.title_input.value.strip()
        cfg[f"{prefix}_embed_description"] = self.desc_input.value.strip()
        cfg[f"{prefix}_embed_color"] = color
        cfg[f"{prefix}_embed_thumbnail"] = self.thumb_input.value.strip() or None

        save_config()
        await interaction.response.send_message(
            "Embed configuration updated.", ephemeral=True
        )


# ============================================================
# AutoMod configuration UI
# ============================================================

AUTOMOD_LABELS = {
    "anti_links": "Anti-Links",
    "anti_invites": "Anti-Discord-Invites",
    "anti_spam": "Anti-Spam",
    "anti_caps": "Anti-Caps",
    "word_filter": "Word Filter",
}


class AutoModView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(AutoModSelect(guild_id))


class AutoModSelect(discord.ui.Select):
    def __init__(self, guild_id):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description="View/change this AutoMod feature.",
            )
            for key, label in AUTOMOD_LABELS.items()
        ]
        super().__init__(
            placeholder="Choose an AutoMod feature...",
            options=options,
        )

    async def callback(self, interaction):
        key = self.values[0]
        cfg = get_automod_config(self.guild_id)[key]

        enabled = "ON" if cfg.get("enabled") else "OFF"
        embed = discord.Embed(
            title=f"{AUTOMOD_LABELS[key]} — {enabled}",
            color=0x2ECC71 if cfg.get("enabled") else 0x7F8C8D,
        )

        if key == "anti_links":
            embed.description = (
                f"**Block all links:** {cfg.get('block_all_links')}\n"
                f"**Allowed domains:** {', '.join(cfg.get('allowed_domains', [])) or 'None'}\n"
                f"**Delete:** {cfg.get('delete_message')}\n"
                f"**Warn:** {cfg.get('warn_user')}\n"
                f"**Timeout after:** {cfg.get('timeout_after', 0)} violations"
            )
        elif key == "anti_invites":
            embed.description = (
                f"**Delete:** {cfg.get('delete_message')}\n"
                f"**Warn:** {cfg.get('warn_user')}\n"
                f"**Timeout after:** {cfg.get('timeout_after', 0)} violations"
            )
        elif key == "anti_spam":
            embed.description = (
                f"**Message limit:** {cfg.get('message_limit')}\n"
                f"**Window:** {cfg.get('window_seconds')} seconds\n"
                f"**Duplicate limit:** {cfg.get('duplicate_limit')}\n"
                f"**Delete:** {cfg.get('delete_messages')}\n"
                f"**Warn:** {cfg.get('warn_user')}\n"
                f"**Timeout after:** {cfg.get('timeout_after', 0)} violations"
            )
        elif key == "anti_caps":
            embed.description = (
                f"**Minimum length:** {cfg.get('minimum_length')}\n"
                f"**Caps percentage:** {cfg.get('caps_percentage')}%\n"
                f"**Delete:** {cfg.get('delete_message')}\n"
                f"**Warn:** {cfg.get('warn_user')}"
            )
        else:
            embed.description = (
                f"**Blocked words:** {len(cfg.get('blocked_words', []))}\n"
                f"**Delete:** {cfg.get('delete_message')}\n"
                f"**Warn:** {cfg.get('warn_user')}"
            )

        view = AutoModFeatureView(self.guild_id, key)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AutoModFeatureView(discord.ui.View):
    def __init__(self, guild_id, feature):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.feature = feature
        self.add_item(AutoModToggleButton(guild_id, feature))
        self.add_item(AutoModEditButton(guild_id, feature))


class AutoModToggleButton(discord.ui.Button):
    def __init__(self, guild_id, feature):
        self.guild_id = guild_id
        self.feature = feature
        super().__init__(label="Toggle ON/OFF", style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        cfg = get_automod_config(self.guild_id)[self.feature]
        cfg["enabled"] = not cfg.get("enabled", False)
        save_config()
        state = "enabled" if cfg["enabled"] else "disabled"
        await interaction.response.send_message(
            f"{AUTOMOD_LABELS[self.feature]} is now **{state}**.",
            ephemeral=True,
        )


class AutoModEditButton(discord.ui.Button):
    def __init__(self, guild_id, feature):
        self.guild_id = guild_id
        self.feature = feature
        super().__init__(label="Edit Settings", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        await interaction.response.send_modal(
            AutoModModal(self.guild_id, self.feature)
        )


class AutoModModal(discord.ui.Modal):
    def __init__(self, guild_id, feature):
        self.guild_id = guild_id
        self.feature = feature
        cfg = get_automod_config(guild_id)[feature]
        super().__init__(title=f"Configure {AUTOMOD_LABELS[feature]}")

        if feature == "anti_links":
            self.add_item(TextField(
                "Allowed domains",
                ",".join(cfg.get("allowed_domains", [])),
                "Example: roblox.com,youtube.com",
            ))
            self.add_item(TextField(
                "Block all links? (yes/no)",
                "yes" if cfg.get("block_all_links") else "no",
                "Use yes to block all except allowed domains.",
            ))
            self.add_item(TextField(
                "Warn user? (yes/no)",
                "yes" if cfg.get("warn_user") else "no",
                "Automatic warning.",
            ))
            self.add_item(TextField(
                "Timeout after violations",
                str(cfg.get("timeout_after", 0)),
                "0 disables escalation.",
            ))
            self.add_item(TextField(
                "Timeout minutes",
                str(cfg.get("timeout_minutes", 10)),
                "Used when escalation triggers.",
            ))

        elif feature == "anti_invites":
            self.add_item(TextField(
                "Warn user? (yes/no)",
                "yes" if cfg.get("warn_user") else "no",
                "Automatic warning.",
            ))
            self.add_item(TextField(
                "Timeout after violations",
                str(cfg.get("timeout_after", 0)),
                "0 disables escalation.",
            ))
            self.add_item(TextField(
                "Timeout minutes",
                str(cfg.get("timeout_minutes", 10)),
                "Used when escalation triggers.",
            ))

        elif feature == "anti_spam":
            self.add_item(TextField(
                "Messages allowed",
                str(cfg.get("message_limit", 5)),
                "Example: 5",
            ))
            self.add_item(TextField(
                "Time window (seconds)",
                str(cfg.get("window_seconds", 5)),
                "Example: 5",
            ))
            self.add_item(TextField(
                "Duplicate limit",
                str(cfg.get("duplicate_limit", 3)),
                "3 means the same message 3 times triggers.",
            ))
            self.add_item(TextField(
                "Warn user? (yes/no)",
                "yes" if cfg.get("warn_user") else "no",
                "Automatic warning.",
            ))
            self.add_item(TextField(
                "Timeout after violations",
                str(cfg.get("timeout_after", 3)),
                "0 disables escalation.",
            ))

        elif feature == "anti_caps":
            self.add_item(TextField(
                "Minimum message length",
                str(cfg.get("minimum_length", 10)),
                "Letters below this length are ignored.",
            ))
            self.add_item(TextField(
                "Caps percentage",
                str(cfg.get("caps_percentage", 70)),
                "70 = 70% uppercase letters.",
            ))
            self.add_item(TextField(
                "Warn user? (yes/no)",
                "yes" if cfg.get("warn_user") else "no",
                "Automatic warning.",
            ))

        else:
            self.add_item(TextField(
                "Blocked words (comma separated)",
                ",".join(cfg.get("blocked_words", [])),
                "Example: word1,word2,word3",
            ))
            self.add_item(TextField(
                "Warn user? (yes/no)",
                "yes" if cfg.get("warn_user") else "no",
                "Automatic warning.",
            ))

    async def on_submit(self, interaction):
        cfg = get_automod_config(self.guild_id)[self.feature]

        def yes(value):
            return value.strip().lower() in {"yes", "y", "true", "on", "1"}

        def integer(value, low, high):
            value = int(value.strip())
            if not low <= value <= high:
                raise ValueError
            return value

        try:
            values = [x.value for x in self.children]

            if self.feature == "anti_links":
                cfg["allowed_domains"] = [
                    normalize_domain(x) for x in values[0].split(",") if x.strip()
                ]
                cfg["block_all_links"] = yes(values[1])
                cfg["warn_user"] = yes(values[2])
                cfg["timeout_after"] = integer(values[3], 0, 100)
                cfg["timeout_minutes"] = integer(values[4], 1, 40320)

            elif self.feature == "anti_invites":
                cfg["warn_user"] = yes(values[0])
                cfg["timeout_after"] = integer(values[1], 0, 100)
                cfg["timeout_minutes"] = integer(values[2], 1, 40320)

            elif self.feature == "anti_spam":
                cfg["message_limit"] = integer(values[0], 2, 100)
                cfg["window_seconds"] = integer(values[1], 1, 60)
                cfg["duplicate_limit"] = integer(values[2], 0, 10)
                cfg["warn_user"] = yes(values[3])
                cfg["timeout_after"] = integer(values[4], 0, 100)

            elif self.feature == "anti_caps":
                cfg["minimum_length"] = integer(values[0], 3, 500)
                cfg["caps_percentage"] = integer(values[1], 50, 100)
                cfg["warn_user"] = yes(values[2])

            else:
                words = []
                for word in values[0].split(","):
                    word = word.strip().casefold()
                    if word and len(word) <= 100:
                        words.append(word)
                cfg["blocked_words"] = list(dict.fromkeys(words))
                cfg["warn_user"] = yes(values[1])

            save_config()
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "One or more values are invalid. Please check the numbers and try again.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"{AUTOMOD_LABELS[self.feature]} settings saved.",
            ephemeral=True,
        )


class TextField(discord.ui.TextInput):
    def __init__(self, label, default, placeholder):
        super().__init__(
            label=label,
            default=str(default),
            placeholder=placeholder,
            required=False,
            max_length=4000,
        )


# ============================================================
# Events
# ============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Persistent ticket buttons survive bot restarts.
    bot.add_view(CreateTicketView())
    bot.add_view(TicketManageView())

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"Slash-command sync failed: {e}")

    print("XModda is ready.")


@bot.event
async def on_message(message):
    # AutoMod runs before prefix commands.
    if message.guild and not message.author.bot:
        try:
            handled = await run_automod(message)
            if handled:
                return
        except Exception as e:
            print(f"AutoMod error: {type(e).__name__}: {e}")

    await bot.process_commands(message)


# ============================================================
# Prefix command
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} slash commands globally.")
    except Exception as e:
        await ctx.send(f"Sync failed: `{e}`")


# ============================================================
# Ticket commands
# ============================================================

@bot.tree.command(name="ticket_config", description="Open the ticket configuration panel.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def ticket_config(interaction):
    await interaction.response.send_message(
        "Select an option to configure tickets:",
        view=ConfigMainView(),
        ephemeral=True,
    )


# ============================================================
# AutoMod command
# ============================================================

@bot.tree.command(name="automod", description="Configure XModda AutoMod.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def automod(interaction):
    cfg = get_automod_config(interaction.guild.id)

    embed = discord.Embed(
        title="XModda AutoMod",
        description="Choose a feature below to view or configure it.",
        color=0x5865F2,
    )

    for key, label in AUTOMOD_LABELS.items():
        embed.add_field(
            name=label,
            value="🟢 ON" if cfg[key].get("enabled") else "⚪ OFF",
            inline=True,
        )

    await interaction.response.send_message(
        embed=embed,
        view=AutoModView(interaction.guild.id),
        ephemeral=True,
    )


# ============================================================
# Utility commands
# ============================================================

@bot.tree.command(name="userinfo", description="Get information about a user.")
@app_commands.guild_only()
async def userinfo(interaction, user: discord.Member | None = None):
    user = user or interaction.user
    embed = discord.Embed(
        title=f"User Info: {user}",
        color=0x3498DB,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Bot?", value=str(user.bot), inline=True)
    embed.add_field(name="Nickname", value=user.nick or "None", inline=True)
    embed.add_field(
        name="Joined Server",
        value=user.joined_at.strftime("%Y-%m-%d %H:%M")
        if user.joined_at else "N/A",
        inline=True,
    )
    roles = [r.mention for r in user.roles[1:]]
    embed.add_field(name="Roles", value=", ".join(roles) or "None", inline=False)
    embed.add_field(
        name="Account Created",
        value=user.created_at.strftime("%Y-%m-%d %H:%M"),
        inline=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="serverinfo", description="Get information about the server.")
@app_commands.guild_only()
async def serverinfo(interaction):
    guild = interaction.guild
    owner = guild.owner.mention if guild.owner else "Unknown"
    embed = discord.Embed(
        title=guild.name,
        color=0x9B59B6,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=owner, inline=True)
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(
        name="Created",
        value=guild.created_at.strftime("%Y-%m-%d"),
        inline=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="avatar", description="Get the avatar of a user.")
@app_commands.guild_only()
async def avatar(interaction, user: discord.User | None = None):
    user = user or interaction.user
    embed = discord.Embed(
        title=f"{user}'s Avatar",
        color=0xE67E22,
    )
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# Moderation commands
# ============================================================

@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.guild_only()
async def kick(interaction, user: discord.Member, reason: str = "No reason provided"):
    if user == interaction.user or not can_bot_moderate(user):
        return await interaction.response.send_message(
            "I cannot kick that member because of the role hierarchy.",
            ephemeral=True,
        )

    if user.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You cannot kick a member with an equal or higher role.",
            ephemeral=True,
        )

    dm_sent = await send_dm_embed(user, "kicked", reason, interaction.guild.name)

    try:
        await user.kick(reason=reason)
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(
            f"Failed to kick: `{e}`", ephemeral=True
        )

    await log_event(
        interaction.guild,
        "Member Kicked",
        f"{user.mention} was kicked by {interaction.user.mention}.\nReason: {reason}",
        0xE74C3C,
    )

    embed = discord.Embed(
        title="Member Kicked",
        description=f"{user.mention} has been kicked.",
        color=0xE74C3C,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.guild_only()
async def ban(interaction, user: discord.Member, reason: str = "No reason provided"):
    if user == interaction.user or not can_bot_moderate(user):
        return await interaction.response.send_message(
            "I cannot ban that member because of the role hierarchy.",
            ephemeral=True,
        )

    if user.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You cannot ban a member with an equal or higher role.",
            ephemeral=True,
        )

    dm_sent = await send_dm_embed(user, "banned", reason, interaction.guild.name)

    try:
        await user.ban(reason=reason)
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(
            f"Failed to ban: `{e}`", ephemeral=True
        )

    await log_event(
        interaction.guild,
        "Member Banned",
        f"{user.mention} was banned by {interaction.user.mention}.\nReason: {reason}",
        0xE74C3C,
    )

    embed = discord.Embed(
        title="Member Banned",
        description=f"{user.mention} has been banned.",
        color=0xE74C3C,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.guild_only()
async def timeout(
    interaction,
    user: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided",
):
    if user == interaction.user or not can_bot_moderate(user):
        return await interaction.response.send_message(
            "I cannot timeout that member because of the role hierarchy.",
            ephemeral=True,
        )

    if user.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You cannot timeout a member with an equal or higher role.",
            ephemeral=True,
        )

    dm_sent = await send_dm_embed(user, "timed out", reason, interaction.guild.name)

    try:
        await user.timeout(dt.timedelta(minutes=minutes), reason=reason)
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(
            f"Failed to timeout: `{e}`", ephemeral=True
        )

    await log_event(
        interaction.guild,
        "Member Timed Out",
        f"{user.mention} was timed out for **{minutes} minutes** by "
        f"{interaction.user.mention}.\nReason: {reason}",
        0xE67E22,
    )

    embed = discord.Embed(
        title="Member Timed Out",
        description=f"{user.mention} has been timed out for {minutes} minutes.",
        color=0xE67E22,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="purge", description="Delete messages.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def purge(
    interaction,
    amount: app_commands.Range[int, 1, 100],
):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.followup.send(
            f"Failed to purge messages: `{e}`", ephemeral=True
        )

    await log_event(
        interaction.guild,
        "Messages Purged",
        f"{interaction.user.mention} deleted {len(deleted)} messages in "
        f"{interaction.channel.mention}.",
        0x3498DB,
    )
    await interaction.followup.send(
        f"Deleted {len(deleted)} messages.", ephemeral=True
    )


@bot.tree.command(name="warn", description="Warn a member.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def warn(
    interaction,
    user: discord.Member,
    reason: str = "No reason provided",
):
    if user == interaction.user:
        return await interaction.response.send_message(
            "You cannot warn yourself.", ephemeral=True
        )

    dm_sent = await send_dm_embed(user, "warned", reason, interaction.guild.name)
    count = await add_warning(
        interaction.guild,
        user,
        interaction.user.id,
        reason,
    )

    await log_event(
        interaction.guild,
        "Member Warned",
        f"{user.mention} was warned by {interaction.user.mention}.\n"
        f"Reason: {reason}\nWarnings: {count}",
        0xF1C40F,
    )

    embed = discord.Embed(
        title="Member Warned",
        description=f"{user.mention} has been warned.",
        color=0xF1C40F,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Warning Count", value=str(count), inline=False)
    embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnings", description="View warnings for a user.")
@app_commands.guild_only()
async def warnings_cmd(interaction, user: discord.Member):
    entries = get_warning_list(interaction.guild.id, user.id)

    if not entries:
        return await interaction.response.send_message(
            f"{user.mention} has no warnings.", ephemeral=True
        )

    embed = discord.Embed(
        title=f"Warnings for {user}",
        color=0xE74C3C,
    )

    for i, entry in enumerate(entries[-25:], 1):
        warner = interaction.guild.get_member(int(entry.get("warned_by", 0)))
        name = warner.mention if warner else "Unknown"
        embed.add_field(
            name=f"Warning {i}",
            value=(
                f"**Reason:** {entry.get('reason', 'Unknown')}\n"
                f"**By:** {name}\n"
                f"**Time:** {entry.get('timestamp', 'Unknown')}"
            )[:1024],
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Clear all warnings for a user.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def clearwarnings(interaction, user: discord.Member):
    guild_key = str(interaction.guild.id)
    user_key = str(user.id)

    if guild_key in warnings:
        warnings[guild_key].pop(user_key, None)
        save_warnings()

    await log_event(
        interaction.guild,
        "Warnings Cleared",
        f"Warnings for {user.mention} were cleared by {interaction.user.mention}.",
        0x3498DB,
    )

    await interaction.response.send_message(
        f"Cleared all warnings for {user.mention}.",
        ephemeral=True,
    )


# ============================================================
# App-command error handling
# ============================================================

@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You don't have permission to use that command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        print(f"Slash command error: {type(error).__name__}: {error}")
        message = "Something went wrong while running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ============================================================
# Startup
# ============================================================

load_config()
load_warnings()

if not os.environ.get("DISCORD_TOKEN"):
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Add it to your Render Environment Variables."
    )

threading.Thread(target=run_web_server, daemon=True).start()

bot.run(os.environ["DISCORD_TOKEN"])
