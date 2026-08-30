import discord
from discord.ext import commands
import asyncio
import os
import datetime
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

# --- Discord bot ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ========== CONFIGURATION ==========
# Replace these with your actual Discord IDs
TICKET_CATEGORY_ID = 123456789012345678          # Category where ticket channels appear
STAFF_ROLE_ID = 123456789012345678              # Role that can see tickets
LOG_CHANNEL_ID = 123456789012345678             # Channel for logs/transcripts
TICKET_PANEL_CHANNEL_ID = 123456789012345678    # Channel where "Create Ticket" button is posted
# ===================================

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    # Start web server in separate thread
    threading.Thread(target=run_web_server, daemon=True).start()
    print('Web server started for UptimeRobot')
    # Send ticket panel button
    channel = bot.get_channel(TICKET_PANEL_CHANNEL_ID)
    if channel:
        # Delete previous bot messages in that channel to avoid duplicates
        async for msg in channel.history(limit=10):
            if msg.author == bot.user:
                await msg.delete()
        view = discord.ui.View(timeout=None)
        button = discord.ui.Button(
            label="Create Ticket",
            style=discord.ButtonStyle.green,
            custom_id="create_ticket"
        )
        view.add_item(button)
        await channel.send("Click the button to open a support ticket:", view=view)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        if interaction.data['custom_id'] == 'create_ticket':
            await create_ticket(interaction)

async def create_ticket(interaction: discord.Interaction):
    guild = interaction.guild
    member = interaction.user
    category = guild.get_channel(TICKET_CATEGORY_ID)
    staff_role = guild.get_role(STAFF_ROLE_ID)

    # Check for existing ticket
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
        title="Ticket Created",
        description=f"Welcome {member.mention}! Please describe your issue. Staff will assist shortly.",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Ticket ID: {ticket_channel.id}")
    await ticket_channel.send(embed=embed)

    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"Ticket {ticket_channel.mention} created by {member.mention}")

    await interaction.response.send_message(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

@bot.command()
@commands.has_permissions(administrator=True)
async def close(ctx):
    """Close the ticket and save transcript."""
    if not ctx.channel.name.startswith('ticket-'):
        await ctx.send("This is not a ticket channel.")
        return

    transcript = []
    async for message in ctx.channel.history(limit=1000, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{message.author.name}#{message.author.discriminator}"
        content = message.content if message.content else "[Embed/Attachment]"
        transcript.append(f"[{timestamp}] {author}: {content}")

    html_content = "<html><head><title>Ticket Transcript</title></head><body>"
    html_content += "<h1>Ticket Transcript</h1><pre>"
    html_content += "\n".join(transcript)
    html_content += "</pre></body></html>"

    filename = f"transcript-{ctx.channel.name}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    log_channel = ctx.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(f"Transcript for {ctx.channel.mention}:", file=discord.File(filename))

    await ctx.send("Closing ticket...")
    await asyncio.sleep(2)
    await ctx.channel.delete()

    if os.path.exists(filename):
        os.remove(filename)

# Run bot
bot.run(os.environ['DISCORD_TOKEN'])
