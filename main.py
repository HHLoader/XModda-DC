import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import datetime
import json
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
DEFAULT_CONFIG = {
    "ticket_category_id": None,
    "staff_role_id": None,
    "log_channel_id": None,
    "panel_embed_title": "Support Tickets",
    "panel_embed_description": "Click the button below to open a support ticket.",
    "panel_embed_color": 0x00ff00,
    "opened_embed_title": "Ticket Created",
    "opened_embed_description": "Welcome {user}! Please describe your issue. Staff will assist shortly.\n\nUse the button below to close this ticket.",
    "opened_embed_color": 0x0000ff
}

config = DEFAULT_CONFIG.copy()

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

load_config()

# --- Persistent Views ---
class CreateTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_ticket(interaction)

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(view=self)

        countdown_msg = await interaction.channel.send("Closing ticket in 5 seconds...")
        for i in range(5, 0, -1):
            await asyncio.sleep(1)
            await countdown_msg.edit(content=f"Closing ticket in {i} seconds...")

        await save_transcript(interaction.channel, interaction.guild)
        await interaction.channel.delete()

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

    existing = discord.utils.get(guild.text_channels, name=f'ticket-{member.name.lower()}')
    if existing:
        await interaction.response.send_message("You already have an open ticket!", ephemeral=True)
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

    close_view = CloseTicketView()
    await ticket_channel.send(embed=embed, view=close_view)

    log_channel_id = config.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"Ticket {ticket_channel.mention} created by {member.mention}")

    await interaction.response.send_message(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

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
    bot.add_view(CloseTicketView())

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

# --- Main Config Command with Dropdown ---
@bot.tree.command(name="ticket_config", description="Open ticket configuration panel (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_config(interaction: discord.Interaction):
    view = ConfigMainView()
    await interaction.response.send_message("Select an option to configure:", view=view, ephemeral=True)

class ConfigMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ConfigMainSelect())

class ConfigMainSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Set Category", description="Set the category for new tickets", emoji="📁"),
            discord.SelectOption(label="Set Staff Role", description="Set the role that can see tickets", emoji="👥"),
            discord.SelectOption(label="Set Log Channel", description="Set the channel for logs/transcripts", emoji="📜"),
            discord.SelectOption(label="Edit Panel Embed", description="Edit the ticket panel embed", emoji="🎨"),
            discord.SelectOption(label="Edit Opened Embed", description="Edit the opened ticket embed", emoji="🎫"),
            discord.SelectOption(label="Post Ticket Panel", description="Post the ticket panel in this channel", emoji="📬")
        ]
        super().__init__(placeholder="Choose an option...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "Set Category":
            # Show category selection
            categories = interaction.guild.categories
            if not categories:
                await interaction.response.send_message("No categories found.", ephemeral=True)
                return
            options = [
                discord.SelectOption(label=cat.name, value=str(cat.id))
                for cat in categories[:25]  # Discord limit
            ]
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
            roles = interaction.guild.roles
            # Exclude @everyone and maybe bot roles? Just list all
            options = [
                discord.SelectOption(label=role.name, value=str(role.id))
                for role in roles if role.name != "@everyone"
            ][:25]
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
            if not text_channels:
                await interaction.response.send_message("No text channels found.", ephemeral=True)
                return
            options = [
                discord.SelectOption(label=ch.name, value=str(ch.id))
                for ch in text_channels[:25]
            ]
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
        elif selected == "Edit Panel Embed":
            modal = EmbedEditModal(
                title="Edit Panel Embed",
                current_title=config.get("panel_embed_title", ""),
                current_description=config.get("panel_embed_description", ""),
                current_color=hex(config.get("panel_embed_color", 0x00ff00)),
                embed_type="panel"
            )
            await interaction.response.send_modal(modal)
        elif selected == "Edit Opened Embed":
            modal = EmbedEditModal(
                title="Edit Opened Ticket Embed",
                current_title=config.get("opened_embed_title", ""),
                current_description=config.get("opened_embed_description", ""),
                current_color=hex(config.get("opened_embed_color", 0x0000ff)),
                embed_type="opened"
            )
            await interaction.response.send_modal(modal)
        elif selected == "Post Ticket Panel":
            embed = discord.Embed(
                title=config.get("panel_embed_title", "Support Tickets"),
                description=config.get("panel_embed_description", "Click the button below to open a support ticket."),
                color=config.get("panel_embed_color", 0x00ff00)
            )
            view = CreateTicketView()
            await interaction.channel.send(embed=embed, view=view)
            await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

# --- Modal for editing embeds ---
class EmbedEditModal(discord.ui.Modal):
    def __init__(self, title, current_title, current_description, current_color, embed_type):
        super().__init__(title=title)
        self.embed_type = embed_type
        self.add_item(
            discord.ui.TextInput(
                label="Embed Title",
                placeholder="Enter title",
                default=current_title,
                required=False,
                max_length=256
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Embed Description",
                placeholder="Enter description (use {user} for mention in opened ticket)",
                default=current_description,
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=4000
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Embed Color (hex)",
                placeholder="e.g., #00ff00",
                default=current_color,
                required=False,
                max_length=7
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        title_val = self.children[0].value.strip()
        desc_val = self.children[1].value.strip()
        color_val = self.children[2].value.strip()

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
        elif self.embed_type == "opened":
            config["opened_embed_title"] = title_val
            config["opened_embed_description"] = desc_val
            if color_int is not None:
                config["opened_embed_color"] = color_int

        save_config()
        await interaction.response.send_message("Embed configuration updated.", ephemeral=True)

# Run bot
bot.run(os.environ['DISCORD_TOKEN'])
