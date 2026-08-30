import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import datetime
import json
import random
import threading
from flask import Flask

# --- Web server for UptimeRobot ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Alive"

def run_web_server():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# --- Discord bot setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

CONFIG_FILE = 'config.json'
WARNINGS_FILE = 'warnings.json'
DEFAULT_CONFIG = {
    "ticket_category_id": None,
    "staff_role_id": None,
    "log_channel_id": None,
    "panel_embed_title": "Support Tickets",
    "panel_embed_description": "Click the button below to open a support ticket.",
    "panel_embed_color": 0x00ff00,
    "panel_embed_thumbnail": None,
    "opened_embed_title": "Ticket Created",
    "opened_embed_description": "Welcome {user}! Please describe your issue. Staff will assist shortly.\n\nUse the buttons below to manage this ticket.",
    "opened_embed_color": 0x0000ff,
    "opened_embed_thumbnail": None,
    "ticket_limit": 1
}

config = DEFAULT_CONFIG.copy()
warnings = {}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        config = DEFAULT_CONFIG.copy()
        save_config()
    except json.JSONDecodeError:
        config = DEFAULT_CONFIG.copy()

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def load_warnings():
    global warnings
    try:
        with open(WARNINGS_FILE, 'r') as f:
            warnings = json.load(f)
    except FileNotFoundError:
        warnings = {}
    except json.JSONDecodeError:
        warnings = {}

def save_warnings():
    with open(WARNINGS_FILE, 'w') as f:
        json.dump(warnings, f, indent=4)

load_config()
load_warnings()

# --- Persistent Views ---
class CreateTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction)

class TicketManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="claim_ticket")
    async def claim_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await claim_ticket(interaction)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)

async def create_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    member = interaction.user

    category_id = config.get("ticket_category_id")
    staff_role_id = config.get("staff_role_id")
    if category_id is None or staff_role_id is None:
        await interaction.response.send_message("Ticket category or staff role not set. Admins must use `/ticket_config` first.", ephemeral=True)
        return

    category = guild.get_channel(category_id)
    staff_role = guild.get_role(staff_role_id)
    if category is None or staff_role is None:
        await interaction.response.send_message("Configured category or role no longer exists. Please update with `/ticket_config`.", ephemeral=True)
        return

    limit = config.get("ticket_limit", 1)
    open_tickets = [ch for ch in guild.text_channels if ch.name.startswith(f'ticket-{member.name.lower()}') and ch.category_id == category_id]
    if len(open_tickets) >= limit:
        await interaction.response.send_message(f"You already have {limit} open ticket(s). Please close existing ones before creating a new one.", ephemeral=True)
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        staff_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    ticket_channel = await guild.create_text_channel(
        name=f'ticket-{member.name.lower()}',
        category=category,
        overwrites=overwrites
    )

    embed = discord.Embed(
        title=config.get("opened_embed_title", "Ticket Created"),
        description=config.get("opened_embed_description", "Welcome {user}!").replace("{user}", member.mention),
        color=config.get("opened_embed_color", 0x0000ff),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")
    thumbnail = config.get("opened_embed_thumbnail")
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    view = TicketManageView()
    await ticket_channel.send(embed=embed, view=view)

    log_channel_id = config.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"Ticket {ticket_channel.mention} created by {member.mention}")

    await interaction.response.send_message(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

async def claim_ticket(interaction: discord.Interaction):
    staff_role_id = config.get("staff_role_id")
    if staff_role_id is None:
        await interaction.response.send_message("Staff role not set.", ephemeral=True)
        return
    staff_role = interaction.guild.get_role(staff_role_id)
    if staff_role is None or staff_role not in interaction.user.roles:
        await interaction.response.send_message("You are not staff.", ephemeral=True)
        return

    channel = interaction.channel
    if not channel.name.startswith('ticket-'):
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return

    overwrite = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    await channel.set_permissions(interaction.user, overwrite=overwrite)

    async for msg in channel.history(limit=10, oldest_first=True):
        if msg.author == bot.user and msg.embeds:
            embed = msg.embeds[0]
            embed.add_field(name="Claimed by", value=interaction.user.mention, inline=False)
            await msg.edit(embed=embed)
            break

    await interaction.response.send_message("You have claimed this ticket.", ephemeral=True)

async def close_ticket(interaction: discord.Interaction):
    countdown_msg = await interaction.channel.send("Closing ticket in 5 seconds...")
    for i in range(5, 0, -1):
        await asyncio.sleep(1)
        await countdown_msg.edit(content=f"Closing ticket in {i} seconds...")

    await save_transcript(interaction.channel, interaction.guild)
    await interaction.channel.delete()

async def save_transcript(channel, guild):
    transcript_lines = []
    async for message in channel.history(limit=1000, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{message.author.name}#{message.author.discriminator}"
        content = message.content if message.content else "[Embed/Attachment]"
        transcript_lines.append(f"[{timestamp}] {author}: {content}")

    html_content = "<html><head><title>Ticket Transcript</title></head><body>"
    html_content += "<h1>Ticket Transcript</h1><pre>"
    html_content += "\n".join(transcript_lines)
    html_content += "</pre></body></html>"

    filename = f"transcript-{channel.name}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    log_channel_id = config.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"Transcript for `{channel.name}`:", file=discord.File(filename))

    if os.path.exists(filename):
        os.remove(filename)

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    threading.Thread(target=run_web_server, daemon=True).start()
    print('Web server started for UptimeRobot')

    bot.add_view(CreateTicketView())
    bot.add_view(TicketManageView())

    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} commands')
    except Exception as e:
        print(f'Failed to sync commands: {e}')

    print('Bot is ready.')

# --- Prefix sync command ---
@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    try:
        await bot.tree.sync()
        await ctx.send("Slash commands synced globally.")
    except Exception as e:
        await ctx.send(f"Sync failed: {e}")

# --- Ticket Config Dropdown Command ---
@bot.tree.command(name="ticket_config", description="Open ticket configuration panel (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_config(interaction: discord.Interaction):
    view = ConfigMainView()
    await interaction.response.send_message("Select an option to configure:", view=view, ephemeral=True)

class ConfigMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ConfigMainSelect())

class ConfigMainSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="View Current Config", description="Show current settings", emoji="📋"),
            discord.SelectOption(label="Set Category", description="Set the category for new tickets", emoji="📁"),
            discord.SelectOption(label="Set Staff Role", description="Set the role that can see tickets", emoji="👥"),
            discord.SelectOption(label="Set Log Channel", description="Set the channel for logs/transcripts", emoji="📜"),
            discord.SelectOption(label="Set Ticket Limit", description="Max tickets per user", emoji="🔢"),
            discord.SelectOption(label="Edit Panel Embed", description="Edit the ticket panel embed", emoji="🎨"),
            discord.SelectOption(label="Edit Opened Embed", description="Edit the opened ticket embed", emoji="🎫"),
            discord.SelectOption(label="Post Ticket Panel", description="Post the ticket panel in this channel", emoji="📬")
        ]
        super().__init__(placeholder="Choose an option...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "View Current Config":
            embed = discord.Embed(title="Current Ticket Configuration", color=0x00ff00)
            embed.add_field(name="Category", value=f"<#{config.get('ticket_category_id')}>" if config.get('ticket_category_id') else "Not set", inline=False)
            embed.add_field(name="Staff Role", value=f"<@&{config.get('staff_role_id')}>" if config.get('staff_role_id') else "Not set", inline=False)
            embed.add_field(name="Log Channel", value=f"<#{config.get('log_channel_id')}>" if config.get('log_channel_id') else "Not set", inline=False)
            embed.add_field(name="Ticket Limit", value=config.get('ticket_limit', 1), inline=False)
            embed.add_field(name="Panel Embed Title", value=config.get('panel_embed_title', 'Support Tickets'), inline=False)
            embed.add_field(name="Panel Embed Description", value=config.get('panel_embed_description', 'Click the button below to open a support ticket.'), inline=False)
            embed.add_field(name="Opened Embed Title", value=config.get('opened_embed_title', 'Ticket Created'), inline=False)
            embed.add_field(name="Opened Embed Description", value=config.get('opened_embed_description', 'Welcome {user}!'), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        elif selected == "Set Category":
            categories = interaction.guild.categories
            if not categories:
                await interaction.response.send_message("No categories found.", ephemeral=True)
                return
            options = [discord.SelectOption(label=cat.name, value=str(cat.id)) for cat in categories[:25]]
            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(placeholder="Select a category...", options=options)
            async def category_callback(interaction: discord.Interaction):
                cat_id = int(select.values[0])
                config["ticket_category_id"] = cat_id
                save_config()
                cat = interaction.guild.get_channel(cat_id)
                await interaction.response.send_message(f"Ticket category set to {cat.mention}", ephemeral=True)
            select.callback = category_callback
            view.add_item(select)
            await interaction.response.send_message("Select a category:", view=view, ephemeral=True)
        elif selected == "Set Staff Role":
            roles = [r for r in interaction.guild.roles if r.name != "@everyone"]
            options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles[:25]]
            if not options:
                await interaction.response.send_message("No roles available.", ephemeral=True)
                return
            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(placeholder="Select a role...", options=options)
            async def role_callback(interaction: discord.Interaction):
                role_id = int(select.values[0])
                config["staff_role_id"] = role_id
                save_config()
                role = interaction.guild.get_role(role_id)
                await interaction.response.send_message(f"Staff role set to {role.mention}", ephemeral=True)
            select.callback = role_callback
            view.add_item(select)
            await interaction.response.send_message("Select a role:", view=view, ephemeral=True)
        elif selected == "Set Log Channel":
            text_channels = [ch for ch in interaction.guild.channels if isinstance(ch, discord.TextChannel)]
            options = [discord.SelectOption(label=ch.name, value=str(ch.id)) for ch in text_channels[:25]]
            if not options:
                await interaction.response.send_message("No text channels found.", ephemeral=True)
                return
            view = discord.ui.View(timeout=60)
            select = discord.ui.Select(placeholder="Select a log channel...", options=options)
            async def log_callback(interaction: discord.Interaction):
                ch_id = int(select.values[0])
                config["log_channel_id"] = ch_id
                save_config()
                ch = interaction.guild.get_channel(ch_id)
                await interaction.response.send_message(f"Log channel set to {ch.mention}", ephemeral=True)
            select.callback = log_callback
            view.add_item(select)
            await interaction.response.send_message("Select a log channel:", view=view, ephemeral=True)
        elif selected == "Set Ticket Limit":
            modal = LimitModal()
            await interaction.response.send_modal(modal)
        elif selected == "Edit Panel Embed":
            modal = EmbedEditModal(
                title="Edit Panel Embed",
                current_title=config.get("panel_embed_title", ""),
                current_description=config.get("panel_embed_description", ""),
                current_color=hex(config.get("panel_embed_color", 0x00ff00)),
                current_thumbnail=config.get("panel_embed_thumbnail"),
                embed_type="panel"
            )
            await interaction.response.send_modal(modal)
        elif selected == "Edit Opened Embed":
            modal = EmbedEditModal(
                title="Edit Opened Ticket Embed",
                current_title=config.get("opened_embed_title", ""),
                current_description=config.get("opened_embed_description", ""),
                current_color=hex(config.get("opened_embed_color", 0x0000ff)),
                current_thumbnail=config.get("opened_embed_thumbnail"),
                embed_type="opened"
            )
            await interaction.response.send_modal(modal)
        elif selected == "Post Ticket Panel":
            embed = discord.Embed(
                title=config.get("panel_embed_title", "Support Tickets"),
                description=config.get("panel_embed_description", "Click the button below to open a support ticket."),
                color=config.get("panel_embed_color", 0x00ff00)
            )
            if config.get("panel_embed_thumbnail"):
                embed.set_thumbnail(url=config["panel_embed_thumbnail"])
            embed.set_footer(text="Powered by SOMBRA")
            view = CreateTicketView()
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

# --- Modal for ticket limit ---
class LimitModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Set Ticket Limit")
        self.add_item(discord.ui.TextInput(label="Limit", placeholder="Enter max tickets per user (1-10)", default=str(config.get('ticket_limit', 1)), max_length=2))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.children[0].value)
            if 1 <= limit <= 10:
                config["ticket_limit"] = limit
                save_config()
                await interaction.response.send_message(f"Ticket limit set to {limit}.", ephemeral=True)
            else:
                await interaction.response.send_message("Limit must be between 1 and 10.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Invalid number.", ephemeral=True)

# --- Modal for editing embeds (with thumbnail) ---
class EmbedEditModal(discord.ui.Modal):
    def __init__(self, title, current_title, current_description, current_color, current_thumbnail, embed_type):
        super().__init__(title=title)
        self.embed_type = embed_type
        self.add_item(discord.ui.TextInput(label="Embed Title", placeholder="Enter title", default=current_title, required=False, max_length=256))
        self.add_item(discord.ui.TextInput(label="Embed Description", placeholder="Enter description (use {user} for mention in opened ticket)", default=current_description, style=discord.TextStyle.paragraph, required=False, max_length=4000))
        self.add_item(discord.ui.TextInput(label="Embed Color (hex)", placeholder="e.g., #00ff00", default=current_color, required=False, max_length=7))
        self.add_item(discord.ui.TextInput(label="Thumbnail URL", placeholder="Optional image URL", default=current_thumbnail or "", required=False, max_length=500))

    async def on_submit(self, interaction: discord.Interaction):
        title_val = self.children[0].value.strip()
        desc_val = self.children[1].value.strip()
        color_val = self.children[2].value.strip()
        thumb_val = self.children[3].value.strip()

        try:
            color_int = int(color_val.lstrip('#'), 16) if color_val else None
        except ValueError:
            color_int = None
            await interaction.response.send_message("Invalid color hex. Using default.", ephemeral=True)

        if self.embed_type == "panel":
            config["panel_embed_title"] = title_val
            config["panel_embed_description"] = desc_val
            if color_int is not None:
                config["panel_embed_color"] = color_int
            if thumb_val:
                config["panel_embed_thumbnail"] = thumb_val
            else:
                config["panel_embed_thumbnail"] = None
        elif self.embed_type == "opened":
            config["opened_embed_title"] = title_val
            config["opened_embed_description"] = desc_val
            if color_int is not None:
                config["opened_embed_color"] = color_int
            if thumb_val:
                config["opened_embed_thumbnail"] = thumb_val
            else:
                config["opened_embed_thumbnail"] = None

        save_config()
        await interaction.response.send_message("Embed configuration updated.", ephemeral=True)

# --- Non-ticket slash commands (moderation & utility) ---
@bot.tree.command(name="userinfo", description="Get information about a user")
async def userinfo(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    member = interaction.guild.get_member(user.id)
    embed = discord.Embed(title=f"User Info: {user}", color=0x3498db)
    embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
    embed.add_field(name="ID", value=user.id, inline=True)
    embed.add_field(name="Bot?", value=user.bot, inline=True)
    if member:
        embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d %H:%M") if member.joined_at else "N/A", inline=True)
        embed.add_field(name="Roles", value=", ".join([r.mention for r in member.roles[1:]]) or "None", inline=False)
    embed.add_field(name="Account Created", value=user.created_at.strftime("%Y-%m-%d %H:%M"), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverinfo", description="Get information about the server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=0x9b59b6)
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="avatar", description="Get avatar of a user")
async def avatar(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user}'s Avatar", color=0xe67e22)
    embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Moderation commands with DM and embeds ---
async def send_dm_embed(member: discord.Member, action: str, reason: str, guild_name: str):
    """Attempt to send a DM embed to the member before action."""
    embed = discord.Embed(
        title=f"You have been {action}",
        description=f"**Server:** {guild_name}\n**Reason:** {reason}",
        color=0xff0000,
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text="This action was performed by a moderator.")
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

@bot.tree.command(name="kick", description="Kick a member")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot kick a member with equal or higher role.", ephemeral=True)
        return

    dm_sent = await send_dm_embed(user, "kicked", reason, interaction.guild.name)

    try:
        await user.kick(reason=reason)
    except Exception as e:
        await interaction.response.send_message(f"Failed to kick: {e}", ephemeral=True)
        return

    confirm_embed = discord.Embed(
        title="Member Kicked",
        description=f"{user.mention} has been kicked.",
        color=0xff0000,
        timestamp=datetime.datetime.utcnow()
    )
    confirm_embed.add_field(name="Reason", value=reason, inline=False)
    confirm_embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No (DMs closed or error)", inline=False)
    confirm_embed.set_footer(text=f"Moderator: {interaction.user}")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot ban a member with equal or higher role.", ephemeral=True)
        return

    dm_sent = await send_dm_embed(user, "banned", reason, interaction.guild.name)

    try:
        await user.ban(reason=reason)
    except Exception as e:
        await interaction.response.send_message(f"Failed to ban: {e}", ephemeral=True)
        return

    confirm_embed = discord.Embed(
        title="Member Banned",
        description=f"{user.mention} has been banned.",
        color=0xff0000,
        timestamp=datetime.datetime.utcnow()
    )
    confirm_embed.add_field(name="Reason", value=reason, inline=False)
    confirm_embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No (DMs closed or error)", inline=False)
    confirm_embed.set_footer(text=f"Moderator: {interaction.user}")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot timeout a member with equal or higher role.", ephemeral=True)
        return

    dm_sent = await send_dm_embed(user, "timed out", reason, interaction.guild.name)

    duration = datetime.timedelta(minutes=minutes)
    try:
        await user.timeout(duration, reason=reason)
    except Exception as e:
        await interaction.response.send_message(f"Failed to timeout: {e}", ephemeral=True)
        return

    confirm_embed = discord.Embed(
        title="Member Timed Out",
        description=f"{user.mention} has been timed out for {minutes} minutes.",
        color=0xff0000,
        timestamp=datetime.datetime.utcnow()
    )
    confirm_embed.add_field(name="Reason", value=reason, inline=False)
    confirm_embed.add_field(name="DM Sent", value="Yes" if dm_sent else "No (DMs closed or error)", inline=False)
    confirm_embed.set_footer(text=f"Moderator: {interaction.user}")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

@bot.tree.command(name="purge", description="Delete messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)

@bot.tree.command(name="warn", description="Warn a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    user_id = str(user.id)
    if user_id not in warnings:
        warnings[user_id] = []
    warnings[user_id].append({"reason": reason, "warned_by": interaction.user.id, "timestamp": datetime.datetime.utcnow().isoformat()})
    save_warnings()
    await interaction.response.send_message(f"Warned {user.mention}. They now have {len(warnings[user_id])} warning(s).", ephemeral=True)

@bot.tree.command(name="warnings", description="View warnings for a user")
async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
    user_id = str(user.id)
    if user_id not in warnings or not warnings[user_id]:
        await interaction.response.send_message(f"{user.mention} has no warnings.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Warnings for {user}", color=0xff0000)
    for i, warn in enumerate(warnings[user_id], 1):
        warner = interaction.guild.get_member(warn["warned_by"])
        embed.add_field(name=f"Warning {i}", value=f"Reason: {warn['reason']}\nWarned by: {warner.mention if warner else 'Unknown'}\nTime: {warn['timestamp']}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Clear all warnings for a user")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarnings(interaction: discord.Interaction, user: discord.Member):
    user_id = str(user.id)
    if user_id in warnings:
        del warnings[user_id]
        save_warnings()
    await interaction.response.send_message(f"Cleared all warnings for {user.mention}.", ephemeral=True)

# Run bot
bot.run(os.environ['DISCORD_TOKEN'])
