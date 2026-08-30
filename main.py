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

# Configuration file
CONFIG_FILE = 'config.json'
DEFAULT_CONFIG = {
    "ticket_category_id": None,
    "staff_role_id": None,
    "log_channel_id": None
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
        # Disable button immediately to prevent multiple clicks
        button.disabled = True
        await interaction.response.edit_message(view=self)

        # Send countdown message
        countdown_msg = await interaction.channel.send("Closing ticket in 5 seconds...")
        await asyncio.sleep(5)

        # Save transcript
        await save_transcript(interaction.channel, interaction.guild)

        # Delete channel
        await interaction.channel.delete()

async def create_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    member = interaction.user

    # Check config
    category_id = config.get("ticket_category_id")
    staff_role_id = config.get("staff_role_id")

    if category_id is None:
        await interaction.response.send_message("Ticket category is not set. An admin must use `/set_ticket_category` first.", ephemeral=True)
        return
    if staff_role_id is None:
        await interaction.response.send_message("Staff role is not set. An admin must use `/set_staff_role` first.", ephemeral=True)
        return

    category = guild.get_channel(category_id)
    staff_role = guild.get_role(staff_role_id)

    if category is None:
        await interaction.response.send_message("Configured category no longer exists. Please update with `/set_ticket_category`.", ephemeral=True)
        return
    if staff_role is None:
        await interaction.response.send_message("Configured staff role no longer exists. Please update with `/set_staff_role`.", ephemeral=True)
        return

    # Check for existing open ticket (same naming convention)
    existing = discord.utils.get(guild.text_channels, name=f'ticket-{member.name.lower()}')
    if existing:
        await interaction.response.send_message("You already have an open ticket!", ephemeral=True)
        return

    # Create channel
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

    # Initial message with close button
    embed = discord.Embed(
        title="Ticket Created",
        description=f"Welcome {member.mention}! Please describe your issue. Staff will assist shortly.\n\nUse the button below to close this ticket.",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")

    close_view = CloseTicketView()
    await ticket_channel.send(embed=embed, view=close_view)

    # Log creation if log channel set
    log_channel_id = config.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(f"Ticket {ticket_channel.mention} created by {member.mention}")

    await interaction.response.send_message(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

async def save_transcript(channel, guild):
    """Save transcript as HTML and send to log channel if configured."""
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
    # Start web server
    threading.Thread(target=run_web_server, daemon=True).start()
    print('Web server started for UptimeRobot')

    # Add persistent views to bot
    bot.add_view(CreateTicketView())
    bot.add_view(CloseTicketView())

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} commands')
    except Exception as e:
        print(f'Failed to sync commands: {e}')

    print('Bot is ready.')

# --- Slash Commands ---
@bot.tree.command(name="set_ticket_category", description="Set the category where new tickets will be created (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    config["ticket_category_id"] = category.id
    save_config()
    await interaction.response.send_message(f"Ticket category set to {category.mention}", ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set the role that can see tickets (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    config["staff_role_id"] = role.id
    save_config()
    await interaction.response.send_message(f"Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set the channel for ticket logs/transcripts (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config["log_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="ticket_panel", description="Post the ticket creation panel with button (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction):
    view = CreateTicketView()
    embed = discord.Embed(
        title="Support Tickets",
        description="Click the button below to open a support ticket.",
        color=discord.Color.green()
    )
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)

# Error handling for slash commands
@set_ticket_category.error
@set_staff_role.error
@set_log_channel.error
@ticket_panel.error
async def command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

# Run bot
bot.run(os.environ['DISCORD_TOKEN'])
