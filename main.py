import os, re, json, asyncio, datetime as dt, logging, threading, html
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

logging.basicConfig(level=logging.INFO, format='[XModda] %(message)s')
log = logging.getLogger('xmodda')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip()
PORT = int(os.getenv('PORT', '8080'))
if not DISCORD_TOKEN:
    raise RuntimeError('DISCORD_TOKEN is missing.')

DEFAULTS = {
    'antiSpam': False, 'antiLinks': False, 'antiInvites': False, 'badWords': False,
    'caps': False, 'duplicate': False, 'raid': False,
    'antiSpamLimit': 5, 'antiSpamWindow': 6, 'duplicateLimit': 3, 'duplicateWindow': 20,
    'capsPercent': 70, 'capsMinLength': 8, 'raidJoinLimit': 8, 'raidWindow': 10,
    'timeoutMinutes': 5, 'violationTimeoutThreshold': 3, 'badWordsList': [],
    'ignoredChannels': [], 'bypassRoles': [], 'logging': False, 'logChannelId': '',
    'logModeration': True, 'logAutoMod': True, 'logJoins': False, 'logLeaves': False,
    'welcome': False, 'welcomeChannelId': '',
    'welcomeMessage': 'Welcome {user} to **{server}**! You are member #{count}.',
    'goodbye': False, 'goodbyeChannelId': '', 'goodbyeMessage': '**{username}** has left {server}.',
    'autoRole': False, 'autoRoleId': '', 'autoRoleIds': [], 'tickets': True, 'ticketCategoryId': '',
    'ticketStaffRoleId': '', 'ticketStaffRoleIds': [], 'ticketLogChannelId': '', 'ticketPanelChannelId': '',
    'ticketLimit': 1, 'ticketPanelTitle': 'Support Tickets',
    'ticketPanelDescription': 'Click the button below to open a private support ticket.',
    'ticketOpenedTitle': 'Ticket Created',
    'ticketOpenedDescription': 'Welcome {user}! Please describe your issue.',
    'ticketOpenedColor': 0x5865F2, 'ticketOpenedThumbnail': '',
    'ticketPanelThumbnail': '', 'ticketPanelColor': 0x5865F2,
    'disabledCommands': [], 'serverDescription': '',
}

URL_RE = re.compile(r'(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|co|me|tv|dev|xyz|info|site|online|app|ly|us|uk|ca)\b', re.I)
INVITE_RE = re.compile(r'(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+', re.I)

settings_cache = {}
settings_cache_at = {}
SETTINGS_TTL = 5
spam_history = defaultdict(deque)
duplicate_history = defaultdict(deque)
violations = defaultdict(deque)
join_history = defaultdict(deque)
ticket_claims = {}

health = Flask(__name__)
@health.get('/')
def health_root():
    return 'XModda is online'

def run_health():
    health.run(host='0.0.0.0', port=PORT)

# ---------------- Supabase ----------------
def db_headers():
    if not SUPABASE_KEY:
        raise RuntimeError('SUPABASE_SERVICE_ROLE_KEY is missing on this host.')
    # Supabase supports sb_secret_* keys through the apikey header. Legacy service_role JWTs also work here.
    return {'apikey': SUPABASE_KEY, 'Content-Type': 'application/json', 'Accept': 'application/json'}

def db_error(prefix, exc):
    if isinstance(exc, HTTPError):
        try:
            body = exc.read().decode('utf-8', 'replace')
        except Exception:
            body = ''
        return f'{prefix} HTTP {exc.code}: {body[:800]}'
    return f'{prefix}: {exc}'

def db_get_sync(guild_id):
    if not SUPABASE_URL:
        raise RuntimeError('SUPABASE_URL is missing on this host.')
    q = urlencode({'guild_id': f'eq.{guild_id}', 'select': 'settings,updated_at', 'limit': '1'})
    req = Request(f'{SUPABASE_URL}/rest/v1/guild_settings?{q}', headers=db_headers(), method='GET')
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as exc:
        raise RuntimeError(db_error('Supabase GET failed', exc)) from exc

def db_upsert_sync(guild_id, settings):
    if not SUPABASE_URL:
        raise RuntimeError('SUPABASE_URL is missing on this host.')
    payload = {'guild_id': str(guild_id), 'settings': settings,
               'updated_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    req = Request(
        f'{SUPABASE_URL}/rest/v1/guild_settings',
        headers={**db_headers(), 'Prefer': 'resolution=merge-duplicates,return=representation'},
        data=json.dumps(payload).encode('utf-8'), method='POST'
    )
    try:
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
            return data[0] if data else payload
    except Exception as exc:
        raise RuntimeError(db_error('Supabase UPSERT failed', exc)) from exc

async def get_settings(guild_id, force=False):
    now = asyncio.get_running_loop().time()
    if not force and guild_id in settings_cache and now - settings_cache_at.get(guild_id, 0) < SETTINGS_TTL:
        return settings_cache[guild_id]
    rows = await asyncio.to_thread(db_get_sync, guild_id)
    raw = rows[0].get('settings') if rows else {}
    if not isinstance(raw, dict): raw = {}
    merged = {**DEFAULTS, **raw}
    if not merged.get('ticketStaffRoleIds') and merged.get('ticketStaffRoleId'):
        merged['ticketStaffRoleIds'] = [merged['ticketStaffRoleId']]
    if not merged.get('autoRoleIds') and merged.get('autoRoleId'):
        merged['autoRoleIds'] = [merged['autoRoleId']]
    settings_cache[guild_id] = merged
    settings_cache_at[guild_id] = now
    return merged

async def save_settings(guild_id, changes):
    current = await get_settings(guild_id, force=True)
    current.update(changes)
    row = await asyncio.to_thread(db_upsert_sync, guild_id, current)
    saved = row.get('settings', current) if isinstance(row, dict) else current
    merged = {**DEFAULTS, **saved}
    settings_cache[guild_id] = merged
    settings_cache_at[guild_id] = asyncio.get_running_loop().time()
    return merged

# ---------------- Discord ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

async def command_enabled(interaction: discord.Interaction):
    if not interaction.guild:
        return True
    try:
        s = await get_settings(interaction.guild.id)
    except Exception:
        return True  # never make a database outage prevent diagnostics
    name = getattr(interaction.command, 'name', '')
    return name not in set(str(x) for x in (s.get('disabledCommands') or []))

@bot.tree.interaction_check
async def global_command_check(interaction: discord.Interaction):
    if await command_enabled(interaction):
        return True
    try:
        await interaction.response.send_message('❌ This command has been disabled by the server administrator.', ephemeral=True)
    except discord.HTTPException:
        pass
    return False

def is_staff(member: discord.Member, settings=None):
    p = member.guild_permissions
    if p.administrator or p.manage_guild or p.manage_messages:
        return True
    if settings is None:
        return False
    role_ids = settings.get('ticketStaffRoleIds') or []
    if not role_ids and settings.get('ticketStaffRoleId'):
        role_ids = [settings.get('ticketStaffRoleId')]
    allowed = {int(x) for x in role_ids if str(x).isdigit()}
    return any(role.id in allowed for role in member.roles)

def bypassed(member: discord.Member, settings):
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    ids = {int(x) for x in settings.get('bypassRoles', []) if str(x).isdigit()}
    return any(role.id in ids for role in member.roles)

def ignored(message, settings):
    ids = {int(x) for x in settings.get('ignoredChannels', []) if str(x).isdigit()}
    return message.channel.id in ids

async def send_log(guild, title, description, color=0x5865F2, kind='auto'):
    try:
        s = await get_settings(guild.id)
    except Exception as exc:
        log.error('log settings failed: %s', exc)
        return
    if not s.get('logging'):
        return
    if kind == 'auto' and not s.get('logAutoMod', True): return
    if kind == 'moderation' and not s.get('logModeration', True): return
    if kind == 'join' and not s.get('logJoins', False): return
    if kind == 'leave' and not s.get('logLeaves', False): return
    cid = s.get('ticketLogChannelId') if kind == 'ticket' and s.get('ticketLogChannelId') else s.get('logChannelId')
    ch = guild.get_channel(int(cid)) if cid and str(cid).isdigit() else None
    if not ch: return
    try:
        await ch.send(embed=discord.Embed(title=title, description=description, color=color, timestamp=dt.datetime.now(dt.timezone.utc)))
    except discord.HTTPException as exc:
        log.error('log send failed: %s', exc)

async def violation(message, reason, settings):
    try:
        await message.delete()
    except discord.HTTPException as exc:
        log.warning('could not delete AutoMod message: %s', exc)
    key = (message.guild.id, message.author.id)
    now = asyncio.get_running_loop().time()
    h = violations[key]
    h.append(now)
    while h and now - h[0] > 600: h.popleft()
    try:
        await message.channel.send(f'⚠️ {message.author.mention}, your message was removed: **{reason}**', delete_after=6)
    except discord.HTTPException:
        pass
    threshold = max(1, int(settings.get('violationTimeoutThreshold', 3) or 3))
    minutes = max(0, int(settings.get('timeoutMinutes', 5) or 0))
    me = message.guild.me
    if len(h) >= threshold and minutes and me and me.guild_permissions.moderate_members:
        if isinstance(message.author, discord.Member) and message.author.top_role < me.top_role:
            try:
                await message.author.timeout(dt.timedelta(minutes=minutes), reason=f'XModda AutoMod: {reason}')
            except discord.HTTPException as exc:
                log.warning('AutoMod timeout failed: %s', exc)
    await send_log(message.guild, 'AutoMod action', f'**User:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Reason:** {reason}', 0xED4245, 'auto')

async def automod(message):
    if not message.guild or message.author.bot or not message.content:
        return False
    try:
        settings = await get_settings(message.guild.id)
    except Exception as exc:
        log.error('AutoMod settings load failed for %s: %s', message.guild.id, exc)
        return False
    if bypassed(message.author, settings) or ignored(message, settings):
        return False
    content = message.content
    now = asyncio.get_running_loop().time()
    key = (message.guild.id, message.author.id)
    if settings.get('antiInvites') and INVITE_RE.search(content):
        await violation(message, 'Discord invites are disabled in this server.', settings); return True
    if settings.get('antiLinks') and URL_RE.search(content):
        await violation(message, 'Links are disabled in this server.', settings); return True
    words = [str(w).strip() for w in (settings.get('badWordsList') or []) if str(w).strip()]
    if settings.get('badWords') and any(w.casefold() in content.casefold() for w in words):
        await violation(message, 'A blocked word or phrase was detected.', settings); return True
    if settings.get('caps'):
        letters = [c for c in content if c.isalpha()]
        minimum = max(1, int(settings.get('capsMinLength', 8) or 8))
        pct = (sum(c.isupper() for c in letters) / len(letters) * 100) if letters else 0
        if len(letters) >= minimum and pct >= float(settings.get('capsPercent', 70) or 70):
            await violation(message, 'Excessive uppercase text is not allowed.', settings); return True
    if settings.get('duplicate'):
        d = duplicate_history[key]
        normalized = re.sub(r'\s+', ' ', content.strip().casefold())
        d.append((now, normalized))
        window = max(1, float(settings.get('duplicateWindow', 20) or 20))
        while d and now - d[0][0] > window: d.popleft()
        if sum(1 for _, text in d if text == normalized) >= max(2, int(settings.get('duplicateLimit', 3) or 3)):
            await violation(message, 'Repeated duplicate messages are not allowed.', settings); return True
    if settings.get('antiSpam'):
        h = spam_history[key]
        h.append(now)
        window = max(1, int(settings.get('antiSpamWindow', 6) or 6))
        limit = max(1, int(settings.get('antiSpamLimit', 5) or 5))
        while h and now - h[0] > window: h.popleft()
        if len(h) > limit:
            await violation(message, f'Too many messages in {window} seconds.', settings); return True
    return False

# ---------------- Events ----------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        log.info('Logged in as %s | synced %d slash commands', bot.user, len(synced))
    except Exception as exc:
        log.error('slash sync failed: %s', exc)
    log.info('Guilds: %d | Supabase configured: %s', len(bot.guilds), bool(SUPABASE_URL and SUPABASE_KEY))
    if bot.guilds:
        try:
            await get_settings(bot.guilds[0].id, force=True)
            log.info('Supabase connectivity: OK')
        except Exception as exc:
            log.error('Supabase connectivity: FAILED -> %s', exc)

@bot.event
async def on_message(message):
    if message.guild and not message.author.bot:
        await automod(message)
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    try:
        s = await get_settings(member.guild.id)
    except Exception as exc:
        log.error('join settings load failed: %s', exc); return
    if s.get('welcome') and s.get('welcomeChannelId'):
        ch = member.guild.get_channel(int(s['welcomeChannelId'])) if str(s['welcomeChannelId']).isdigit() else None
        if ch:
            text = str(s.get('welcomeMessage') or DEFAULTS['welcomeMessage'])
            text = text.replace('{user}', member.mention).replace('{username}', member.name).replace('{server}', member.guild.name).replace('{count}', str(member.guild.member_count or 0))
            try: await ch.send(text)
            except discord.HTTPException as exc: log.warning('welcome failed: %s', exc)
    if s.get('autoRole') and s.get('autoRoleId'):
        role = member.guild.get_role(int(s['autoRoleId'])) if str(s['autoRoleId']).isdigit() else None
        me = member.guild.me
        if role and me and role < me.top_role and me.guild_permissions.manage_roles:
            try: await member.add_roles(role, reason='XModda auto-role')
            except discord.HTTPException as exc: log.warning('auto-role failed: %s', exc)
    if s.get('raid'):
        now = asyncio.get_running_loop().time(); h = join_history[member.guild.id]; h.append(now)
        window = max(1, int(s.get('raidWindow', 10) or 10)); limit = max(2, int(s.get('raidJoinLimit', 8) or 8))
        while h and now - h[0] > window: h.popleft()
        if len(h) >= limit:
            await send_log(member.guild, '🚨 Possible raid detected', f'{len(h)} members joined within {window} seconds.', 0xED4245, 'auto')
    await send_log(member.guild, 'Member joined', f'{member.mention} joined the server.', 0x57F287, 'join')

@bot.event
async def on_member_remove(member):
    try: s = await get_settings(member.guild.id)
    except Exception as exc: log.error('leave settings load failed: %s', exc); return
    if s.get('goodbye') and s.get('goodbyeChannelId'):
        ch = member.guild.get_channel(int(s['goodbyeChannelId'])) if str(s['goodbyeChannelId']).isdigit() else None
        if ch:
            text = str(s.get('goodbyeMessage') or DEFAULTS['goodbyeMessage']).replace('{username}', member.name).replace('{server}', member.guild.name)
            try: await ch.send(text)
            except discord.HTTPException as exc: log.warning('goodbye failed: %s', exc)
    await send_log(member.guild, 'Member left', f'**Member:** {member} (`{member.id}`)', 0xED4245, 'leave')

# ---------------- Helpers ----------------
def ok(text): return discord.Embed(title='XModda', description='✅ ' + text, color=0x57F287)
def err(text): return discord.Embed(title='XModda', description='❌ ' + text, color=0xED4245)

def require_guild(i):
    return i.guild is not None

# ---------------- General ----------------
@bot.tree.command(name='ping', description='Check XModda latency')
async def ping(i): await i.response.send_message(f'🏓 Pong! `{round(bot.latency * 1000)}ms`', ephemeral=True)

@bot.tree.command(name='xmodda_diag', description='Test XModda Discord and Supabase connectivity')
@app_commands.checks.has_permissions(manage_guild=True)
async def xmodda_diag(i):
    e = discord.Embed(title='XModda Diagnostics', color=0x5865F2)
    me = i.guild.me; p = me.guild_permissions if me else None
    e.add_field(name='Discord Gateway', value='🟢 Connected', inline=True)
    e.add_field(name='Message Content', value='🟢 Enabled in code', inline=True)
    e.add_field(name='Members Intent', value='🟢 Enabled in code', inline=True)
    try:
        s = await get_settings(i.guild.id, force=True)
        e.add_field(name='Supabase', value='🟢 Connected', inline=True)
        e.add_field(name='Settings', value='🟢 Loaded', inline=True)
        e.add_field(name='AutoMod', value=f"Links {'🟢' if s.get('antiLinks') else '🔴'} | Spam {'🟢' if s.get('antiSpam') else '🔴'} | Invites {'🟢' if s.get('antiInvites') else '🔴'}", inline=False)
    except Exception as exc:
        e.add_field(name='Supabase', value='🔴 FAILED', inline=True); e.add_field(name='Error', value=str(exc)[:900], inline=False)
    if p:
        e.add_field(name='Bot Permissions', value=f"Manage Messages: {'🟢' if p.manage_messages else '🔴'}\nModerate Members: {'🟢' if p.moderate_members else '🔴'}\nManage Roles: {'🟢' if p.manage_roles else '🔴'}\nManage Channels: {'🟢' if p.manage_channels else '🔴'}", inline=False)
    await i.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name='serverinfo', description='Show server information')
async def serverinfo(i):
    g=i.guild; e=discord.Embed(title=g.name,color=0x5865F2); e.add_field(name='Members',value=str(g.member_count)); e.add_field(name='Channels',value=str(len(g.channels))); e.add_field(name='Owner',value=f'<@{g.owner_id}>'); await i.response.send_message(embed=e)

@bot.tree.command(name='userinfo', description='Show information about a member')
async def userinfo(i, member: discord.Member):
    e=discord.Embed(title=f'User Info — {member}',color=member.color.value or 0x5865F2); e.set_thumbnail(url=member.display_avatar.url); e.add_field(name='ID',value=str(member.id),inline=False); e.add_field(name='Joined',value=discord.utils.format_dt(member.joined_at,'R') if member.joined_at else 'Unknown'); e.add_field(name='Roles',value=', '.join(r.mention for r in member.roles[1:]) or 'None',inline=False); await i.response.send_message(embed=e)

@bot.tree.command(name='avatar', description="Show a member's avatar")
async def avatar(i, member: Optional[discord.Member] = None):
    member=member or i.user; e=discord.Embed(title=f"{member.display_name}'s Avatar",color=0x5865F2); e.set_image(url=member.display_avatar.url); await i.response.send_message(embed=e)

# ---------------- Moderation ----------------
@bot.tree.command(name='purge', description='Delete messages')
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(i, amount: app_commands.Range[int,1,100]):
    deleted=await i.channel.purge(limit=amount); await i.response.send_message(f'🗑️ Deleted {len(deleted)} messages.',ephemeral=True); await send_log(i.guild,'Messages purged',f'{i.user.mention} deleted {len(deleted)} messages in {i.channel.mention}',kind='moderation')

@bot.tree.command(name='kick', description='Kick a member')
@app_commands.checks.has_permissions(kick_members=True)
async def kick(i, member: discord.Member, reason: str='No reason provided'):
    if member == i.user or member.top_role >= i.guild.me.top_role: return await i.response.send_message(embed=err('I cannot kick that member because of role hierarchy.'),ephemeral=True)
    await member.kick(reason=reason); await i.response.send_message(f'👢 Kicked **{member}**.'); await send_log(i.guild,'Member kicked',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xED4245,'moderation')

@bot.tree.command(name='ban', description='Ban a member')
@app_commands.checks.has_permissions(ban_members=True)
async def ban(i, member: discord.Member, reason: str='No reason provided'):
    if member == i.user or member.top_role >= i.guild.me.top_role: return await i.response.send_message(embed=err('I cannot ban that member because of role hierarchy.'),ephemeral=True)
    await member.ban(reason=reason); await i.response.send_message(f'🔨 Banned **{member}**.'); await send_log(i.guild,'Member banned',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xED4245,'moderation')

@bot.tree.command(name='timeout', description='Timeout a member')
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(i, member: discord.Member, minutes: app_commands.Range[int,1,40320], reason: str='No reason provided'):
    if member == i.user or member.top_role >= i.guild.me.top_role: return await i.response.send_message(embed=err("I can't timeout that member because of role hierarchy."),ephemeral=True)
    await member.timeout(dt.timedelta(minutes=minutes),reason=reason); await i.response.send_message(f'⏱️ Timed out **{member}** for `{minutes}` minutes.'); await send_log(i.guild,'Member timed out',f'**Member:** {member}\n**By:** {i.user.mention}\n**Duration:** {minutes}m\n**Reason:** {reason}',0xED4245,'moderation')

@bot.tree.command(name='warn', description='Warn a member')
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(i, member: discord.Member, reason: str='No reason provided'):
    key=f'warnings_{i.guild.id}'; s=await get_settings(i.guild.id,True); wm=s.get(key,{}) or {}; uid=str(member.id); wm[uid]=int(wm.get(uid,0))+1; await save_settings(i.guild.id,{key:wm}); await i.response.send_message(f'⚠️ Warned **{member}**. Warnings: `{wm[uid]}`'); await send_log(i.guild,'Member warned',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xFEE75C,'moderation')

@bot.tree.command(name='warnings', description="View a member's warning count")
async def warnings_cmd(i, member: discord.Member):
    s=await get_settings(i.guild.id); wm=s.get(f'warnings_{i.guild.id}',{}) or {}; await i.response.send_message(f'⚠️ **{member}** has `{wm.get(str(member.id),0)}` warning(s).',ephemeral=True)

@bot.tree.command(name='clearwarnings', description="Clear a member's warnings")
@app_commands.checks.has_permissions(manage_messages=True)
async def clearwarnings(i, member: discord.Member):
    s=await get_settings(i.guild.id,True); wm=s.get(f'warnings_{i.guild.id}',{}) or {}; wm.pop(str(member.id),None); await save_settings(i.guild.id,{f'warnings_{i.guild.id}':wm}); await i.response.send_message(f'✅ Cleared warnings for **{member}**.',ephemeral=True)

# ---------------- AutoMod ----------------
@bot.tree.command(name='automod_status', description='Show the live AutoMod settings')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_status(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    e=discord.Embed(title='XModda AutoMod Status',color=0x5865F2)
    for k,l in [('antiLinks','Anti-Links'),('antiInvites','Anti-Invites'),('antiSpam','Anti-Spam'),('duplicate','Duplicate'),('caps','Excessive Caps'),('badWords','Bad Words'),('raid','Raid Protection')]: e.add_field(name=l,value='🟢 ON' if s.get(k) else '🔴 OFF',inline=True)
    e.add_field(name='Spam',value=f"{s['antiSpamLimit']} msgs / {s['antiSpamWindow']}s",inline=True); e.add_field(name='Duplicate',value=f"{s['duplicateLimit']} repeats / {s['duplicateWindow']}s",inline=True); e.add_field(name='Caps',value=f"{s['capsPercent']}% / {s['capsMinLength']} letters",inline=True); await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='automod_reload', description='Reload this server settings from Supabase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_reload(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    await i.response.send_message(f"🔄 Settings reloaded. Anti-Links: **{'ON' if s.get('antiLinks') else 'OFF'}**, Anti-Spam: **{'ON' if s.get('antiSpam') else 'OFF'}**, Anti-Invites: **{'ON' if s.get('antiInvites') else 'OFF'}**.",ephemeral=True)

@bot.tree.command(name='automod_word', description='Add a blocked word or phrase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word(i, word: str):
    try:
        s=await get_settings(i.guild.id,True); words=[str(x) for x in (s.get('badWordsList') or [])]
        if word.casefold() not in [x.casefold() for x in words]: words.append(word)
        await save_settings(i.guild.id,{'badWords':True,'badWordsList':words})
        await i.response.send_message(f'✅ Added `{word}` to blocked words.',ephemeral=True)
    except Exception as ex: await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)

@bot.tree.command(name='automod_word_remove', description='Remove a blocked word or phrase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word_remove(i, word: str):
    try:
        s=await get_settings(i.guild.id,True); words=[x for x in (s.get('badWordsList') or []) if str(x).casefold()!=word.casefold()]; await save_settings(i.guild.id,{'badWordsList':words}); await i.response.send_message(f'✅ Removed `{word}` if it existed.',ephemeral=True)
    except Exception as ex: await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)

# ---------------- Welcome / Roles / Logging ----------------
@bot.tree.command(name='welcome_config', description='Configure welcome messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_config(i, channel: discord.TextChannel, message: str=DEFAULTS['welcomeMessage']):
    try: await save_settings(i.guild.id,{'welcome':True,'welcomeChannelId':channel.id,'welcomeMessage':message})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save welcome settings: {ex}'),ephemeral=True)
    await i.response.send_message(embed=ok(f'Welcome enabled in {channel.mention}.\nMessage: {message}'),ephemeral=True)

@bot.tree.command(name='welcome_disable', description='Disable welcome messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_disable(i): await save_settings(i.guild.id,{'welcome':False}); await i.response.send_message('✅ Welcome disabled.',ephemeral=True)

@bot.tree.command(name='goodbye_config', description='Configure goodbye messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def goodbye_config(i, channel: discord.TextChannel, message: str=DEFAULTS['goodbyeMessage']):
    try: await save_settings(i.guild.id,{'goodbye':True,'goodbyeChannelId':channel.id,'goodbyeMessage':message})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save goodbye settings: {ex}'),ephemeral=True)
    await i.response.send_message(embed=ok(f'Goodbye enabled in {channel.mention}.\nMessage: {message}'),ephemeral=True)

@bot.tree.command(name='goodbye_disable', description='Disable goodbye messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def goodbye_disable(i): await save_settings(i.guild.id,{'goodbye':False}); await i.response.send_message('✅ Goodbye messages disabled.',ephemeral=True)

@bot.tree.command(name='autorole', description='Set the automatic member role')
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole(i, role: discord.Role):
    if i.guild.me and role >= i.guild.me.top_role: return await i.response.send_message(embed=err("That role must be below XModda's highest role."),ephemeral=True)
    try: await save_settings(i.guild.id,{'autoRole':True,'autoRoleId':role.id,'autoRoleIds':[role.id]})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save auto-role settings: {ex}'),ephemeral=True)
    await i.response.send_message(f'✅ Auto-role enabled: {role.mention}',ephemeral=True)

@bot.tree.command(name='autorole_disable', description='Disable automatic roles')
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_disable(i): await save_settings(i.guild.id,{'autoRole':False}); await i.response.send_message('✅ Auto-role disabled.',ephemeral=True)

@bot.tree.command(name='logging_config', description='Set the moderation log channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_config(i, channel: discord.TextChannel):
    try: await save_settings(i.guild.id,{'logging':True,'logChannelId':channel.id})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save logging settings: {ex}'),ephemeral=True)
    await i.response.send_message(f'✅ Logging enabled in {channel.mention}.',ephemeral=True)

@bot.tree.command(name='logging_disable', description='Disable logging')
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_disable(i): await save_settings(i.guild.id,{'logging':False}); await i.response.send_message('✅ Logging disabled.',ephemeral=True)

# ---------------- Tickets ----------------
def _id_list(settings, plural_key, single_key):
    values = settings.get(plural_key) or []
    if not values and settings.get(single_key):
        values = [settings.get(single_key)]
    return [int(x) for x in values if str(x).isdigit()]

def _ticket_staff_roles(guild, settings):
    result = []
    for rid in _id_list(settings, 'ticketStaffRoleIds', 'ticketStaffRoleId'):
        role = guild.get_role(rid)
        if role:
            result.append(role)
    return result

def _ticket_log_channel(guild, settings):
    cid = settings.get('ticketLogChannelId') or settings.get('logChannelId')
    return guild.get_channel(int(cid)) if cid and str(cid).isdigit() else None

def _ticket_text(value, member, guild):
    return str(value or '').replace('{user}', member.mention).replace('{username}', member.name).replace('{server}', guild.name).replace('{count}', str(guild.member_count or 0))

async def open_ticket(i):
    g = i.guild
    if not g or not isinstance(i.user, discord.Member):
        return await i.response.send_message('❌ Tickets can only be opened inside a server.', ephemeral=True)
    try:
        s = await get_settings(g.id, True)
    except Exception as ex:
        return await i.response.send_message(embed=err(f'Database error: {ex}'), ephemeral=True)
    if not s.get('tickets', True):
        return await i.response.send_message('❌ Tickets are currently disabled.', ephemeral=True)
    category = g.get_channel(int(s['ticketCategoryId'])) if str(s.get('ticketCategoryId', '')).isdigit() else None
    staff_roles = _ticket_staff_roles(g, s)
    if not isinstance(category, discord.CategoryChannel) or not staff_roles:
        return await i.response.send_message('❌ Tickets are not configured yet. Choose a category and at least one staff role in the dashboard.', ephemeral=True)
    existing = [c for c in category.channels if isinstance(c, discord.TextChannel) and c.topic == f'xmodda-ticket:{i.user.id}']
    limit = max(1, int(s.get('ticketLimit', 1) or 1))
    if len(existing) >= limit:
        return await i.response.send_message(f'❌ You already have the maximum of {limit} open ticket(s).', ephemeral=True)
    overwrites = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                  i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
    if g.me:
        overwrites[g.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    safe_name = re.sub(r'[^a-z0-9-]+', '-', i.user.name.lower()).strip('-')[:70] or 'user'
    try:
        ch = await g.create_text_channel(f'ticket-{safe_name}'[:95], category=category, overwrites=overwrites,
                                         topic=f'xmodda-ticket:{i.user.id}', reason='XModda ticket opened')
        embed = discord.Embed(title=str(s.get('ticketOpenedTitle') or 'Ticket Created'),
            description=_ticket_text(s.get('ticketOpenedDescription') or 'Welcome {user}! Please describe your issue.', i.user, g),
            color=int(s.get('ticketOpenedColor', 0x5865F2) or 0x5865F2), timestamp=dt.datetime.now(dt.timezone.utc))
        embed.set_footer(text=f'Ticket ID: {ch.id}')
        thumb = str(s.get('ticketOpenedThumbnail') or '').strip()
        if thumb: embed.set_thumbnail(url=thumb)
        embed.add_field(name='Status', value='🟢 Open', inline=True)
        embed.add_field(name='Opened by', value=i.user.mention, inline=True)
        await ch.send(embed=embed, view=TicketManageView())
        await i.response.send_message(f'✅ Ticket created: {ch.mention}', ephemeral=True)
        logch = _ticket_log_channel(g, s)
        if logch:
            await logch.send(embed=discord.Embed(title='🎫 Ticket Opened', description=f'{i.user.mention} opened {ch.mention}.',
                                                 color=0x5865F2, timestamp=dt.datetime.now(dt.timezone.utc)))
    except discord.HTTPException as ex:
        try:
            if 'ch' in locals(): await ch.delete(reason='XModda ticket setup failed')
        except Exception: pass
        if not i.response.is_done():
            await i.response.send_message(embed=err(f'Could not create the ticket: {ex}'), ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Open Ticket', style=discord.ButtonStyle.primary, emoji='🎫', custom_id='xmodda_ticket_open_v3')
    async def open_button(self, i, button): await open_ticket(i)

class TicketManageView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Claim', style=discord.ButtonStyle.success, emoji='🙋', custom_id='xmodda:ticket_claim')
    async def claim(self, i, button):
        if not isinstance(i.user, discord.Member): return await i.response.send_message('❌ Staff only.', ephemeral=True)
        try: s = await get_settings(i.guild.id, True)
        except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'), ephemeral=True)
        if not is_staff(i.user, s): return await i.response.send_message('❌ Staff only.', ephemeral=True)
        if not isinstance(i.channel, discord.TextChannel) or not (i.channel.topic or '').startswith('xmodda-ticket:'):
            return await i.response.send_message('❌ This is not an XModda ticket.', ephemeral=True)
        ticket_claims[i.channel.id] = i.user.id
        button.disabled = True
        button.label = f'Claimed by {i.user.display_name}'[:80]
        try: await i.message.edit(view=self)
        except discord.HTTPException: pass
        await i.response.send_message(f'🙋 Ticket claimed by {i.user.mention}.')
        try:
            async for msg in i.channel.history(limit=20, oldest_first=True):
                if msg.author == bot.user and msg.embeds:
                    em = msg.embeds[0]
                    if not any(f.name == 'Claimed by' for f in em.fields):
                        em.add_field(name='Claimed by', value=i.user.mention, inline=False)
                        await msg.edit(embed=em)
                    break
        except discord.HTTPException: pass
    @discord.ui.button(label='Close Ticket', style=discord.ButtonStyle.danger, emoji='🔒', custom_id='xmodda:ticket_close')
    async def close(self, i, button):
        if not isinstance(i.user, discord.Member): return await i.response.send_message('❌ You cannot close this ticket.', ephemeral=True)
        try: s = await get_settings(i.guild.id, True)
        except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'), ephemeral=True)
        owner_id = (i.channel.topic or '').replace('xmodda-ticket:', '', 1) if i.channel else ''
        if not (is_staff(i.user, s) or str(i.user.id) == owner_id):
            return await i.response.send_message('❌ You cannot close this ticket.', ephemeral=True)
        await i.response.send_message('🔒 Closing ticket in 5 seconds...', ephemeral=True)
        try:
            countdown = await i.channel.send('🔒 Closing ticket in **5** seconds...')
            for remaining in range(4, 0, -1):
                await asyncio.sleep(1)
                await countdown.edit(content=f'🔒 Closing ticket in **{remaining}** seconds...')
            await save_transcript(i.channel, i.guild, s, i.user)
            logch = _ticket_log_channel(i.guild, s)
            if logch:
                await logch.send(embed=discord.Embed(title='🔒 Ticket Closed', description=f'{i.channel.mention} was closed by {i.user.mention}',
                                                     color=0xED4245, timestamp=dt.datetime.now(dt.timezone.utc)))
            await i.channel.delete(reason='XModda ticket closed')
        except discord.HTTPException as ex:
            try: await i.channel.send(f'❌ Could not close this ticket: `{ex}`')
            except Exception: pass

class LegacyTicketPanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Create Ticket', style=discord.ButtonStyle.success, emoji='🎫', custom_id='create_ticket')
    async def create_button(self, i, button): await open_ticket(i)

class LegacyTicketManageView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Claim', style=discord.ButtonStyle.primary, custom_id='claim_ticket')
    async def claim_button(self, i, button): await TicketManageView.claim(self, i, button)
    @discord.ui.button(label='Close Ticket', style=discord.ButtonStyle.danger, custom_id='close_ticket')
    async def close_button(self, i, button): await TicketManageView.close(self, i, button)

async def save_transcript(channel, guild, settings, closer=None):
    logch = _ticket_log_channel(guild, settings)
    if not logch: return
    lines = []
    try:
        async for message in channel.history(limit=None, oldest_first=True):
            stamp = message.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')
            author = f'{message.author} ({message.author.id})'
            content = message.content or ''
            if message.attachments:
                content += (' ' if content else '') + 'Attachments: ' + ', '.join(a.url for a in message.attachments)
            if message.embeds and not content: content = '[Embed]'
            if not content: content = '[No text]'
            lines.append((stamp, author, content))
    except discord.HTTPException:
        lines.append(('unknown', 'system', '[Unable to read the complete channel history.]'))
    safe = re.sub(r'[^a-zA-Z0-9._-]+', '-', channel.name)
    filename = f'transcript-{safe}-{channel.id}.html'
    rows = ''.join('<div class="message"><span class="time">[' + html.escape(stamp) + ']</span> <b>' +
                   html.escape(author) + '</b>: ' + html.escape(content) + '</div>' for stamp, author, content in lines)
    doc = ('<!doctype html><html><head><meta charset="utf-8"><title>Ticket Transcript - ' + html.escape(channel.name) +
           '</title><style>body{font-family:Arial,sans-serif;background:#111;color:#eee;padding:24px}h1{margin:0 0 8px}.meta{color:#aaa;margin-bottom:18px}.message{padding:8px 0;border-bottom:1px solid #2b2b2b;white-space:pre-wrap}.time{color:#888}</style></head><body><h1>Ticket Transcript</h1>' +
           '<div class="meta">Channel: ' + html.escape(channel.name) + ' · Server: ' + html.escape(guild.name) +
           ' · Closed by: ' + html.escape(str(closer) if closer else 'Unknown') + '</div>' + rows + '</body></html>')
    try:
        with open(filename, 'w', encoding='utf-8') as f: f.write(doc)
        await logch.send(content=f'📄 Transcript for **{channel.name}**', file=discord.File(filename))
    finally:
        try: os.remove(filename)
        except OSError: pass

@bot.tree.command(name='ticket_config', description='Configure ticket category and staff role')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_config(i, category: discord.CategoryChannel, staff_role: discord.Role):
    try:
        await save_settings(i.guild.id, {'tickets': True, 'ticketCategoryId': category.id, 'ticketStaffRoleId': staff_role.id, 'ticketStaffRoleIds': [staff_role.id]})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save ticket settings: {ex}'), ephemeral=True)
    await i.response.send_message(f'✅ Tickets configured. Category: **{category.name}** | Staff: {staff_role.mention}', ephemeral=True)

async def send_ticket_panel_to(guild, channel, settings):
    if not isinstance(channel, discord.TextChannel): raise RuntimeError('Panel channel is not a text channel.')
    if not settings.get('ticketCategoryId') or not _id_list(settings, 'ticketStaffRoleIds', 'ticketStaffRoleId'):
        raise RuntimeError('Choose a ticket category and at least one staff role first.')
    embed = discord.Embed(title=str(settings.get('ticketPanelTitle') or 'Support Tickets'),
                          description=str(settings.get('ticketPanelDescription') or 'Click the button below to open a private support ticket.'),
                          color=int(settings.get('ticketPanelColor', 0x5865F2) or 0x5865F2))
    thumb = str(settings.get('ticketPanelThumbnail') or '').strip()
    if thumb: embed.set_thumbnail(url=thumb)
    return await channel.send(embed=embed, view=TicketPanelView())

@bot.tree.command(name='ticket_panel', description='Post a ticket panel in this channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_panel(i):
    try:
        s = await get_settings(i.guild.id, True)
        msg = await send_ticket_panel_to(i.guild, i.channel, s)
        await i.response.send_message(f'✅ Ticket panel sent: {msg.jump_url}', ephemeral=True)
    except Exception as ex: await i.response.send_message(embed=err(str(ex)), ephemeral=True)

@bot.tree.command(name='ticket_setup', description='Post the configured ticket panel')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_setup(i):
    try:
        s = await get_settings(i.guild.id, True)
        cid = s.get('ticketPanelChannelId') or i.channel.id
        ch = i.guild.get_channel(int(cid)) if str(cid).isdigit() else i.channel
        msg = await send_ticket_panel_to(i.guild, ch, s)
        await i.response.send_message(f'✅ Ticket panel sent to {ch.mention}: {msg.jump_url}', ephemeral=True)
    except Exception as ex: await i.response.send_message(embed=err(str(ex)), ephemeral=True)

@bot.tree.command(name='ticket_send', description='Send the configured ticket panel to the selected dashboard channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_send(i):
    try:
        s = await get_settings(i.guild.id, True)
        cid = s.get('ticketPanelChannelId') or i.channel.id
        ch = i.guild.get_channel(int(cid)) if str(cid).isdigit() else i.channel
        msg = await send_ticket_panel_to(i.guild, ch, s)
        await i.response.send_message(f'✅ Ticket panel sent to {ch.mention}: {msg.jump_url}', ephemeral=True)
    except Exception as ex: await i.response.send_message(embed=err(str(ex)), ephemeral=True)

@bot.tree.error
async def on_app_command_error(i,error):
    log.error('Command error: %r', error)
    if isinstance(error, app_commands.MissingPermissions): msg='You do not have the required Discord permission for this command.'
    elif isinstance(error, app_commands.TransformerError): msg='Invalid option. Please choose a valid Discord channel, role, or member.'
    elif isinstance(error, app_commands.CheckFailure): msg='This command cannot be used here or has been disabled.'
    else: msg=f'Command failed: {str(error)[:900]}'
    try:
        if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
        else: await i.response.send_message(msg,ephemeral=True)
    except discord.HTTPException: pass

async def setup_hook():
    # Register persistent views after the Discord client is initialized.
    bot.add_view(TicketPanelView())
    bot.add_view(TicketManageView())
    bot.add_view(LegacyTicketPanelView())
    bot.add_view(LegacyTicketManageView())
    log.info('Persistent ticket views registered.')

bot.setup_hook = setup_hook

def main():
    threading.Thread(target=run_health, daemon=True).start()
    bot.run(DISCORD_TOKEN)

if __name__ == '__main__': main()
