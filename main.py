import os, re, json, asyncio, datetime as dt
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

DISCORD_TOKEN=os.getenv('DISCORD_TOKEN','').strip()
SUPABASE_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPABASE_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','').strip()
PORT=int(os.getenv('PORT','8080'))
if not DISCORD_TOKEN: raise RuntimeError('DISCORD_TOKEN is missing.')

DEFAULTS={
'antiSpam':False,'antiLinks':False,'antiInvites':False,'badWords':False,'caps':False,'duplicate':False,'raid':False,
'antiSpamLimit':5,'antiSpamWindow':6,'duplicateLimit':3,'duplicateWindow':20,'capsPercent':70,'capsMinLength':8,
'raidJoinLimit':8,'raidWindow':10,'timeoutMinutes':5,'violationTimeoutThreshold':3,'badWordsList':[],
'ignoredChannels':[],'bypassRoles':[],'logging':False,'logChannelId':None,'logModeration':True,'logAutoMod':True,
'logJoins':False,'logLeaves':False,'welcome':False,'welcomeChannelId':None,'welcomeMessage':'Welcome {user} to **{server}**! You are member #{count}.',
'goodbye':False,'goodbyeChannelId':None,'goodbyeMessage':'**{username}** has left {server}.','autoRole':False,'autoRoleId':None,
'tickets':True,'ticketCategoryId':None,'ticketStaffRoleId':None,'ticketLogChannelId':None,'ticketPanelChannelId':None,
'ticketLimit':1,'ticketPanelTitle':'Support Tickets','ticketPanelDescription':'Click the button below to open a private support ticket.',
'ticketOpenedTitle':'Ticket Created','ticketOpenedDescription':'Welcome {user}! Please describe your issue.','disabledCommands':[],'serverDescription':''}

URL_RE=re.compile(r'(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|co|me|tv|dev|xyz|info|site|online|app|ly|us|uk|ca)\b',re.I)
INVITE_RE=re.compile(r'(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+',re.I)
settings_cache={}; settings_cache_at={}; SETTINGS_TTL=5
spam_history=defaultdict(deque); duplicate_history=defaultdict(deque); violations=defaultdict(deque); join_history=defaultdict(deque)

health=Flask(__name__)
@health.get('/')
def health_root(): return 'XModda is online'
def run_health(): health.run(host='0.0.0.0',port=PORT)

def db_headers():
    if not SUPABASE_KEY: raise RuntimeError('SUPABASE_SERVICE_ROLE_KEY is missing on this host.')
    return {'apikey':SUPABASE_KEY,'Content-Type':'application/json','Accept':'application/json'}

def db_error(prefix,e):
    if isinstance(e,HTTPError):
        try: body=e.read().decode('utf-8','replace')
        except Exception: body=''
        return f'{prefix} HTTP {e.code}: {body[:500]}'
    return f'{prefix}: {e}'

def db_get_sync(guild_id):
    if not SUPABASE_URL: raise RuntimeError('SUPABASE_URL is missing on this host.')
    q=urlencode({'guild_id':f'eq.{guild_id}','select':'settings','limit':'1'})
    req=Request(f'{SUPABASE_URL}/rest/v1/guild_settings?{q}',headers=db_headers(),method='GET')
    try:
        with urlopen(req,timeout=10) as r: data=json.loads(r.read().decode())
    except Exception as e: raise RuntimeError(db_error('Supabase GET failed',e)) from e
    return (data[0].get('settings') or {}) if data else {}

def db_upsert_sync(guild_id,settings):
    if not SUPABASE_URL: raise RuntimeError('SUPABASE_URL is missing on this host.')
    req=Request(f'{SUPABASE_URL}/rest/v1/guild_settings',headers={**db_headers(),'Prefer':'resolution=merge-duplicates,return=representation'},data=json.dumps({'guild_id':str(guild_id),'settings':settings,'updated_at':dt.datetime.now(dt.timezone.utc).isoformat()}).encode(),method='POST')
    try:
        with urlopen(req,timeout=10) as r: data=json.loads(r.read().decode())
    except Exception as e: raise RuntimeError(db_error('Supabase UPSERT failed',e)) from e
    return data[0].get('settings',settings) if data else settings

async def get_settings(guild_id,force=False):
    now=asyncio.get_running_loop().time()
    if not force and guild_id in settings_cache and now-settings_cache_at.get(guild_id,0)<SETTINGS_TTL: return settings_cache[guild_id]
    raw=await asyncio.to_thread(db_get_sync,guild_id)
    merged={**DEFAULTS,**(raw if isinstance(raw,dict) else {})}
    settings_cache[guild_id]=merged; settings_cache_at[guild_id]=now
    return merged

async def save_settings(guild_id,changes):
    current=await get_settings(guild_id,force=True)
    current.update(changes)
    saved=await asyncio.to_thread(db_upsert_sync,guild_id,current)
    merged={**DEFAULTS,**(saved if isinstance(saved,dict) else current)}
    settings_cache[guild_id]=merged; settings_cache_at[guild_id]=asyncio.get_running_loop().time()
    return merged

intents=discord.Intents.default(); intents.guilds=True; intents.members=True; intents.message_content=True
bot=commands.Bot(command_prefix='!',intents=intents)

def is_staff(member):
    p=member.guild_permissions; return p.administrator or p.manage_guild or p.manage_messages

def bypassed(member,s):
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages: return True
    ids={int(x) for x in s.get('bypassRoles',[]) if str(x).isdigit()}; return any(r.id in ids for r in member.roles)

def ignored(message,s):
    ids={int(x) for x in s.get('ignoredChannels',[]) if str(x).isdigit()}; return message.channel.id in ids

async def send_log(guild,title,description,color=0x5865F2):
    try: s=await get_settings(guild.id)
    except Exception as e: print('[AutoMod] log settings error:',e); return
    if not s.get('logging'): return
    cid=s.get('logChannelId'); ch=guild.get_channel(int(cid)) if cid else None
    if not ch: return
    try: await ch.send(embed=discord.Embed(title=title,description=description,color=color,timestamp=dt.datetime.now(dt.timezone.utc)))
    except discord.HTTPException as e: print('[Logging] send failed:',e)

async def violation(message,reason,s):
    try: await message.delete()
    except discord.HTTPException as e: print('[AutoMod] delete failed:',e)
    key=(message.guild.id,message.author.id); now=asyncio.get_running_loop().time(); h=violations[key]; h.append(now)
    while h and now-h[0]>600: h.popleft()
    try: await message.channel.send(f'⚠️ {message.author.mention}, your message was removed: **{reason}**',delete_after=6)
    except discord.HTTPException: pass
    threshold=max(1,int(s.get('violationTimeoutThreshold',3) or 3)); minutes=max(0,int(s.get('timeoutMinutes',5) or 0))
    if len(h)>=threshold and minutes and message.guild.me and message.guild.me.guild_permissions.moderate_members:
        try: await message.author.timeout(dt.timedelta(minutes=minutes),reason=f'XModda AutoMod: {reason}')
        except discord.HTTPException as e: print('[AutoMod] timeout failed:',e)
    await send_log(message.guild,'AutoMod action',f'**User:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Reason:** {reason}',0xED4245)

async def automod(message):
    if not message.guild or message.author.bot: return False
    if not message.content: return False
    try: s=await get_settings(message.guild.id)
    except Exception as e:
        print('[AutoMod] SETTINGS LOAD FAILED:',e); return False
    if bypassed(message.author,s) or ignored(message,s): return False
    content=message.content; now=asyncio.get_running_loop().time(); key=(message.guild.id,message.author.id)
    if s.get('antiInvites') and INVITE_RE.search(content): await violation(message,'Discord invites are disabled in this server.',s); return True
    if s.get('antiLinks') and URL_RE.search(content): await violation(message,'Links are disabled in this server.',s); return True
    words=[str(w).strip() for w in (s.get('badWordsList') or []) if str(w).strip()]
    if s.get('badWords') and any(w.casefold() in content.casefold() for w in words): await violation(message,'A blocked word or phrase was detected.',s); return True
    if s.get('caps'):
        letters=[c for c in content if c.isalpha()]; minimum=max(1,int(s.get('capsMinLength',8) or 8)); pct=(sum(c.isupper() for c in letters)/len(letters)*100) if letters else 0
        if len(letters)>=minimum and pct>=float(s.get('capsPercent',70) or 70): await violation(message,'Excessive uppercase text is not allowed.',s); return True
    if s.get('duplicate'):
        d=duplicate_history[key]; normalized=re.sub(r'\s+',' ',content.strip().casefold()); d.append((now,normalized))
        while d and now-d[0][0]>float(s.get('duplicateWindow',20) or 20): d.popleft()
        if sum(1 for _,t in d if t==normalized)>=max(2,int(s.get('duplicateLimit',3) or 3)): await violation(message,'Repeated duplicate messages are not allowed.',s); return True
    if s.get('antiSpam'):
        h=spam_history[key]; h.append(now); window=max(1,int(s.get('antiSpamWindow',6) or 6)); limit=max(1,int(s.get('antiSpamLimit',5) or 5))
        while h and now-h[0]>window: h.popleft()
        if len(h)>limit: await violation(message,f'Too many messages in {window} seconds.',s); return True
    return False

@bot.event
async def on_ready():
    try: synced=await bot.tree.sync(); print(f'[XModda] Logged in as {bot.user} | synced {len(synced)} slash commands')
    except Exception as e: print('[XModda] Slash sync failed:',e)
    print(f'[XModda] Guilds: {len(bot.guilds)} | Supabase configured: {bool(SUPABASE_URL and SUPABASE_KEY)}')
    if bot.guilds:
        try: await get_settings(bot.guilds[0].id,force=True); print('[XModda] Supabase connectivity: OK')
        except Exception as e: print('[XModda] Supabase connectivity: FAILED ->',e)

@bot.event
async def on_message(message):
    if message.guild and not message.author.bot: await automod(message)
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    try: s=await get_settings(member.guild.id)
    except Exception as e: print('[Join] settings load failed:',e); return
    if s.get('welcome') and s.get('welcomeChannelId'):
        ch=member.guild.get_channel(int(s['welcomeChannelId']))
        if ch:
            text=str(s.get('welcomeMessage') or DEFAULTS['welcomeMessage']).replace('{user}',member.mention).replace('{username}',member.name).replace('{server}',member.guild.name).replace('{count}',str(member.guild.member_count or 0))
            try: await ch.send(text)
            except discord.HTTPException as e: print('[Welcome] failed:',e)
    if s.get('autoRole') and s.get('autoRoleId'):
        role=member.guild.get_role(int(s['autoRoleId']))
        if role and member.guild.me and role<member.guild.me.top_role:
            try: await member.add_roles(role,reason='XModda auto-role')
            except discord.HTTPException as e: print('[AutoRole] failed:',e)
    if s.get('raid'):
        now=asyncio.get_running_loop().time(); j=join_history[member.guild.id]; j.append(now); window=max(1,int(s.get('raidWindow',10) or 10)); limit=max(2,int(s.get('raidJoinLimit',8) or 8))
        while j and now-j[0]>window: j.popleft()
        if len(j)>=limit: await send_log(member.guild,'🚨 Possible raid detected',f'{len(j)} members joined within {window} seconds.',0xED4245)

@bot.event
async def on_member_remove(member):
    try: s=await get_settings(member.guild.id)
    except Exception as e: print('[Leave] settings load failed:',e); return
    if s.get('goodbye') and s.get('goodbyeChannelId'):
        ch=member.guild.get_channel(int(s['goodbyeChannelId']))
        if ch:
            text=str(s.get('goodbyeMessage') or DEFAULTS['goodbyeMessage']).replace('{username}',member.name).replace('{server}',member.guild.name)
            try: await ch.send(text)
            except discord.HTTPException as e: print('[Goodbye] failed:',e)


def ok(text): return discord.Embed(title='XModda',description='✅ '+text,color=0x57F287)
def err(text): return discord.Embed(title='XModda',description='❌ '+text,color=0xED4245)

@bot.tree.command(name='ping',description='Check XModda latency')
async def ping(i): await i.response.send_message(f'🏓 Pong! `{round(bot.latency*1000)}ms`',ephemeral=True)

@bot.tree.command(name='xmodda_diag',description='Test XModda Discord and Supabase connectivity')
@app_commands.checks.has_permissions(manage_guild=True)
async def xmodda_diag(i):
    e=discord.Embed(title='XModda Diagnostics',color=0x5865F2); me=i.guild.me; p=me.guild_permissions if me else None
    e.add_field(name='Discord Gateway',value='🟢 Connected',inline=True); e.add_field(name='Message Content',value='🟢 Enabled in code',inline=True); e.add_field(name='Members Intent',value='🟢 Enabled in code',inline=True)
    try: s=await get_settings(i.guild.id,force=True); e.add_field(name='Supabase',value='🟢 Connected',inline=True); e.add_field(name='Settings',value='🟢 Loaded',inline=True); e.add_field(name='Anti-Links / Spam',value=f"{'🟢 ON' if s.get('antiLinks') else '🔴 OFF'} / {'🟢 ON' if s.get('antiSpam') else '🔴 OFF'}",inline=True)
    except Exception as ex: e.add_field(name='Supabase',value='🔴 FAILED',inline=True); e.add_field(name='Error',value=str(ex)[:900],inline=False)
    if p: e.add_field(name='Bot Permissions',value=f"Manage Messages: {'🟢' if p.manage_messages else '🔴'}\nModerate Members: {'🟢' if p.moderate_members else '🔴'}\nManage Roles: {'🟢' if p.manage_roles else '🔴'}\nManage Channels: {'🟢' if p.manage_channels else '🔴'}",inline=False)
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='serverinfo',description='Show server information')
async def serverinfo(i):
    g=i.guild; e=discord.Embed(title=g.name,color=0x5865F2); e.add_field(name='Members',value=str(g.member_count)); e.add_field(name='Channels',value=str(len(g.channels))); e.add_field(name='Owner',value=f'<@{g.owner_id}>'); await i.response.send_message(embed=e)

@bot.tree.command(name='userinfo',description='Show information about a member')
async def userinfo(i,member:discord.Member):
    e=discord.Embed(title=f'User Info — {member}',color=member.color.value or 0x5865F2); e.set_thumbnail(url=member.display_avatar.url); e.add_field(name='ID',value=str(member.id),inline=False); e.add_field(name='Joined',value=discord.utils.format_dt(member.joined_at,'R') if member.joined_at else 'Unknown'); await i.response.send_message(embed=e)

@bot.tree.command(name='avatar',description="Show a member's avatar")
async def avatar(i,member:Optional[discord.Member]=None):
    member=member or i.user; e=discord.Embed(title=f"{member.display_name}'s Avatar",color=0x5865F2); e.set_image(url=member.display_avatar.url); await i.response.send_message(embed=e)

@bot.tree.command(name='purge',description='Delete messages')
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(i,amount:app_commands.Range[int,1,100]):
    deleted=await i.channel.purge(limit=amount); await i.response.send_message(f'🗑️ Deleted {len(deleted)} messages.',ephemeral=True); await send_log(i.guild,'Messages purged',f'{i.user.mention} deleted {len(deleted)} messages in {i.channel.mention}')

@bot.tree.command(name='kick',description='Kick a member')
@app_commands.checks.has_permissions(kick_members=True)
async def kick(i,member:discord.Member,reason:str='No reason provided'):
    if member.top_role>=i.user.top_role: return await i.response.send_message(embed=err('You cannot kick this member.'),ephemeral=True)
    await member.kick(reason=reason); await i.response.send_message(f'👢 Kicked **{member}**.'); await send_log(i.guild,'Member kicked',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xED4245)

@bot.tree.command(name='ban',description='Ban a member')
@app_commands.checks.has_permissions(ban_members=True)
async def ban(i,member:discord.Member,reason:str='No reason provided'):
    if member.top_role>=i.user.top_role: return await i.response.send_message(embed=err('You cannot ban this member.'),ephemeral=True)
    await member.ban(reason=reason); await i.response.send_message(f'🔨 Banned **{member}**.'); await send_log(i.guild,'Member banned',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xED4245)

@bot.tree.command(name='timeout',description='Timeout a member')
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(i,member:discord.Member,minutes:app_commands.Range[int,1,40320],reason:str='No reason provided'):
    if member.top_role>=i.guild.me.top_role: return await i.response.send_message(embed=err("I can't timeout that member because of role hierarchy."),ephemeral=True)
    await member.timeout(dt.timedelta(minutes=minutes),reason=reason); await i.response.send_message(f'⏱️ Timed out **{member}** for `{minutes}` minutes.'); await send_log(i.guild,'Member timed out',f'**Member:** {member}\n**By:** {i.user.mention}\n**Duration:** {minutes}m\n**Reason:** {reason}',0xED4245)

@bot.tree.command(name='warn',description='Warn a member')
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(i,member:discord.Member,reason:str='No reason provided'):
    key=f'warnings_{i.guild.id}'; s=await get_settings(i.guild.id,True); wm=s.get(key,{}) or {}; uid=str(member.id); wm[uid]=int(wm.get(uid,0))+1; await save_settings(i.guild.id,{key:wm}); await i.response.send_message(f'⚠️ Warned **{member}**. Warnings: `{wm[uid]}`'); await send_log(i.guild,'Member warned',f'**Member:** {member}\n**By:** {i.user.mention}\n**Reason:** {reason}',0xFEE75C)

@bot.tree.command(name='warnings',description="View a member's warning count")
async def warnings_cmd(i,member:discord.Member):
    s=await get_settings(i.guild.id); wm=s.get(f'warnings_{i.guild.id}',{}) or {}; await i.response.send_message(f'⚠️ **{member}** has `{wm.get(str(member.id),0)}` warning(s).',ephemeral=True)

@bot.tree.command(name='clearwarnings',description="Clear a member's warnings")
@app_commands.checks.has_permissions(manage_messages=True)
async def clearwarnings(i,member:discord.Member):
    s=await get_settings(i.guild.id,True); wm=s.get(f'warnings_{i.guild.id}',{}) or {}; wm.pop(str(member.id),None); await save_settings(i.guild.id,{f'warnings_{i.guild.id}':wm}); await i.response.send_message(f'✅ Cleared warnings for **{member}**.',ephemeral=True)

@bot.tree.command(name='automod_status',description='Show the live AutoMod settings')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_status(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    e=discord.Embed(title='XModda AutoMod Status',color=0x5865F2)
    for k,l in [('antiLinks','Anti-Links'),('antiInvites','Anti-Invites'),('antiSpam','Anti-Spam'),('duplicate','Duplicate'),('caps','Excessive Caps'),('badWords','Bad Words'),('raid','Raid Protection')]: e.add_field(name=l,value='🟢 ON' if s.get(k) else '🔴 OFF',inline=True)
    e.add_field(name='Spam',value=f"{s['antiSpamLimit']} msgs / {s['antiSpamWindow']}s",inline=True); e.add_field(name='Duplicate',value=f"{s['duplicateLimit']} repeats",inline=True); e.add_field(name='Caps',value=f"{s['capsPercent']}% / {s['capsMinLength']} letters",inline=True); await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='automod_reload',description='Reload this server settings from Supabase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_reload(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    await i.response.send_message(f"🔄 Settings reloaded. Anti-Links: **{'ON' if s.get('antiLinks') else 'OFF'}**, Anti-Spam: **{'ON' if s.get('antiSpam') else 'OFF'}**.",ephemeral=True)

@bot.tree.command(name='automod_word',description='Add a blocked word or phrase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word(i,word:str):
    s=await get_settings(i.guild.id,True); words=[str(x) for x in (s.get('badWordsList') or [])];
    if word.casefold() not in [x.casefold() for x in words]: words.append(word)
    await save_settings(i.guild.id,{'badWords':True,'badWordsList':words}); await i.response.send_message(f'✅ Added `{word}` to blocked words.',ephemeral=True)

@bot.tree.command(name='automod_word_remove',description='Remove a blocked word or phrase')
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_word_remove(i,word:str):
    s=await get_settings(i.guild.id,True); words=[x for x in (s.get('badWordsList') or []) if str(x).casefold()!=word.casefold()]; await save_settings(i.guild.id,{'badWordsList':words}); await i.response.send_message(f'✅ Removed `{word}` if it existed.',ephemeral=True)

@bot.tree.command(name='welcome_config',description='Configure welcome messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_config(i,channel:discord.TextChannel,message:str=DEFAULTS['welcomeMessage']):
    try: await save_settings(i.guild.id,{'welcome':True,'welcomeChannelId':channel.id,'welcomeMessage':message})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save welcome settings: {ex}'),ephemeral=True)
    await i.response.send_message(embed=ok(f'Welcome enabled in {channel.mention}.\nMessage: {message}'),ephemeral=True)

@bot.tree.command(name='welcome_disable',description='Disable welcome messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_disable(i): await save_settings(i.guild.id,{'welcome':False}); await i.response.send_message('✅ Welcome disabled.',ephemeral=True)

@bot.tree.command(name='goodbye_config',description='Configure goodbye messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def goodbye_config(i,channel:discord.TextChannel,message:str=DEFAULTS['goodbyeMessage']):
    try: await save_settings(i.guild.id,{'goodbye':True,'goodbyeChannelId':channel.id,'goodbyeMessage':message})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save goodbye settings: {ex}'),ephemeral=True)
    await i.response.send_message(embed=ok(f'Goodbye enabled in {channel.mention}.\nMessage: {message}'),ephemeral=True)

@bot.tree.command(name='goodbye_disable',description='Disable goodbye messages')
@app_commands.checks.has_permissions(manage_guild=True)
async def goodbye_disable(i):
    try: await save_settings(i.guild.id,{'goodbye':False})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save goodbye settings: {ex}'),ephemeral=True)
    await i.response.send_message('✅ Goodbye messages disabled.',ephemeral=True)

@bot.tree.command(name='autorole',description='Set the automatic member role')
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole(i,role:discord.Role):
    if i.guild.me and role>=i.guild.me.top_role: return await i.response.send_message(embed=err("That role must be below XModda's highest role."),ephemeral=True)
    try: await save_settings(i.guild.id,{'autoRole':True,'autoRoleId':role.id})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save auto-role settings: {ex}'),ephemeral=True)
    await i.response.send_message(f'✅ Auto-role enabled: {role.mention}',ephemeral=True)

@bot.tree.command(name='autorole_disable',description='Disable automatic roles')
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_disable(i): await save_settings(i.guild.id,{'autoRole':False}); await i.response.send_message('✅ Auto-role disabled.',ephemeral=True)

@bot.tree.command(name='logging_config',description='Set the moderation log channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_config(i,channel:discord.TextChannel):
    try: await save_settings(i.guild.id,{'logging':True,'logChannelId':channel.id})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save logging settings: {ex}'),ephemeral=True)
    await i.response.send_message(f'✅ Logging enabled in {channel.mention}.',ephemeral=True)

@bot.tree.command(name='logging_disable',description='Disable logging')
@app_commands.checks.has_permissions(manage_guild=True)
async def logging_disable(i): await save_settings(i.guild.id,{'logging':False}); await i.response.send_message('✅ Logging disabled.',ephemeral=True)

class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Open Ticket',style=discord.ButtonStyle.primary,emoji='🎫',custom_id='xmodda:ticket_open')
    async def open_ticket(self,i,button):
        g=i.guild
        try: s=await get_settings(g.id,True)
        except Exception as ex: return await i.response.send_message(f'❌ Database error: {str(ex)[:800]}',ephemeral=True)
        if not s.get('tickets',True): return await i.response.send_message('❌ Tickets are currently disabled.',ephemeral=True)
        category=g.get_channel(int(s['ticketCategoryId'])) if s.get('ticketCategoryId') else None; staff=g.get_role(int(s['ticketStaffRoleId'])) if s.get('ticketStaffRoleId') else None
        if not isinstance(category,discord.CategoryChannel): return await i.response.send_message('❌ Tickets are not configured yet. Set a ticket category first.',ephemeral=True)
        existing=[c for c in category.channels if isinstance(c,discord.TextChannel) and c.topic==f'xmodda-ticket:{i.user.id}']
        limit=max(1,int(s.get('ticketLimit',1) or 1))
        if len(existing)>=limit: return await i.response.send_message(f'❌ You already have the maximum of {limit} open ticket(s).',ephemeral=True)
        ow={g.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),g.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True,read_message_history=True)}
        if staff: ow[staff]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
        ch=await g.create_text_channel(f'ticket-{i.user.name}'[:95],category=category,overwrites=ow,topic=f'xmodda-ticket:{i.user.id}',reason='XModda ticket opened')
        title=str(s.get('ticketOpenedTitle') or 'Ticket Created'); desc=str(s.get('ticketOpenedDescription') or 'Welcome {user}! Please describe your issue.').replace('{user}',i.user.mention).replace('{username}',i.user.name).replace('{server}',g.name)
        await ch.send(embed=discord.Embed(title=title,description=desc,color=0x5865F2),view=CloseTicketView())
        await i.response.send_message(f'✅ Ticket created: {ch.mention}',ephemeral=True); await send_log(g,'Ticket opened',f'{i.user.mention} opened {ch.mention}')

class CloseTicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Close Ticket',style=discord.ButtonStyle.danger,emoji='🔒',custom_id='xmodda:ticket_close')
    async def close_ticket(self,i,button):
        if not (isinstance(i.user,discord.Member) and (is_staff(i.user) or (i.channel.topic or '')==f'xmodda-ticket:{i.user.id}')): return await i.response.send_message('❌ You cannot close this ticket.',ephemeral=True)
        await i.response.send_message('🔒 Closing ticket...',ephemeral=True); await send_log(i.guild,'Ticket closed',f'{i.channel.mention} closed by {i.user.mention}'); await asyncio.sleep(1); await i.channel.delete(reason='XModda ticket closed')

@bot.tree.command(name='ticket_config',description='Configure tickets')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_config(i,category:discord.CategoryChannel,staff_role:discord.Role):
    try: await save_settings(i.guild.id,{'tickets':True,'ticketCategoryId':category.id,'ticketStaffRoleId':staff_role.id})
    except Exception as ex: return await i.response.send_message(embed=err(f'Could not save ticket settings: {ex}'),ephemeral=True)
    await i.response.send_message(f'✅ Tickets configured. Category: **{category.name}** | Staff: {staff_role.mention}',ephemeral=True)

@bot.tree.command(name='ticket_panel',description='Post a ticket panel in this channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_panel(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    if not s.get('ticketCategoryId'): return await i.response.send_message(embed=err('Configure a ticket category first.'),ephemeral=True)
    e=discord.Embed(title=str(s.get('ticketPanelTitle') or 'Support Tickets'),description=str(s.get('ticketPanelDescription') or ''),color=0x5865F2)
    await i.response.send_message(embed=e,view=TicketView())

@bot.tree.command(name='ticket_send',description='Send the configured ticket panel to the configured panel channel')
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_send(i):
    try: s=await get_settings(i.guild.id,True)
    except Exception as ex: return await i.response.send_message(embed=err(f'Database error: {ex}'),ephemeral=True)
    cid=s.get('ticketPanelChannelId') or i.channel.id; ch=i.guild.get_channel(int(cid))
    if not isinstance(ch,discord.TextChannel): return await i.response.send_message(embed=err('Choose a ticket panel channel in the dashboard first.'),ephemeral=True)
    e=discord.Embed(title=str(s.get('ticketPanelTitle') or 'Support Tickets'),description=str(s.get('ticketPanelDescription') or ''),color=0x5865F2)
    msg=await ch.send(embed=e,view=TicketView()); await i.response.send_message(f'✅ Ticket panel sent to {ch.mention}.',ephemeral=True); return msg

@bot.tree.error
async def on_app_command_error(i,error):
    print('[Command Error]',repr(error))
    if isinstance(error,app_commands.MissingPermissions): msg='You do not have the required Discord permission for this command.'
    elif isinstance(error,app_commands.CommandOnCooldown): msg='That command is on cooldown.'
    elif isinstance(error,app_commands.TransformerError): msg='Invalid option. Please choose a valid Discord channel, role, or member.'
    else: msg=f'Command failed: {str(error)[:900]}'
    try:
        if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
        else: await i.response.send_message(msg,ephemeral=True)
    except discord.HTTPException: pass

def main():
    import threading
    threading.Thread(target=run_health,daemon=True).start(); bot.add_view(TicketView()); bot.add_view(CloseTicketView()); bot.run(DISCORD_TOKEN)
if __name__=='__main__': main()
