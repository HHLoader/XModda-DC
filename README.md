# XModda Full Bot

This build connects Discord directly to the same Supabase `guild_settings` row used by the dashboard.

## Render environment variables

Required:
- `DISCORD_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

`SUPABASE_SERVICE_ROLE_KEY` can be the current Supabase `sb_secret_...` server-side key. Never expose it to the browser or use a `NEXT_PUBLIC_` variable.

## Supabase table

Run:

```sql
create table if not exists guild_settings (
  guild_id text primary key,
  settings jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
```

## Discord Developer Portal

Enable:
- Server Members Intent
- Message Content Intent

Presence Intent is not required by this build.

## Bot permissions

Give the bot:
- View Channels
- Send Messages
- Read Message History
- Manage Messages
- Moderate Members
- Manage Channels (tickets)
- Manage Roles (auto-role)
- Kick Members / Ban Members if you want those commands

Put the bot's role above roles it must manage.

## Dashboard contract

The bot understands these JSON keys:

### AutoMod
`antiSpam`, `antiLinks`, `antiInvites`, `badWords`, `caps`, `duplicate`, `raid`, `antiSpamLimit`, `antiSpamWindow`, `duplicateLimit`, `capsPercent`, `capsMinLength`, `raidJoinLimit`, `raidWindow`, `timeoutMinutes`, `badWordsList`, `ignoredChannels`, `bypassRoles`

### Welcome
`welcome`, `welcomeChannelId`, `welcomeMessage`

### Auto-role
`autoRole`, `autoRoleId`

### Logging
`logging`, `logChannelId`

### Tickets
`tickets`, `ticketCategoryId`, `ticketStaffRoleId`, `ticketPanelChannelId`, `ticketLimit`

## Slash commands

General:
- `/ping`
- `/serverinfo`
- `/userinfo`
- `/avatar`

Moderation:
- `/purge`
- `/kick`
- `/ban`
- `/timeout`
- `/warn`
- `/warnings`

AutoMod:
- `/automod_status`
- `/automod_reload`
- `/automod_word`
- `/automod_word_remove`

Server modules:
- `/welcome_config`
- `/welcome_disable`
- `/autorole`
- `/autorole_disable`
- `/logging_config`
- `/logging_disable`

Tickets:
- `/ticket_config`
- `/ticket_panel`

## Testing

After deployment:
1. Run `/automod_status` in the server.
2. Enable Anti-Links on the dashboard.
3. Wait up to 10 seconds, or run `/automod_reload`.
4. Send `https://roblox.com` from a normal member account.
5. The message should be deleted if XModda has `Manage Messages` in that channel.
6. For spam, enable Anti-Spam and send more than the configured limit inside the configured window.

Moderators with `Manage Messages` are intentionally bypassed so admins can test/configure without getting their own messages removed. Use a normal member/alt account for AutoMod tests.
