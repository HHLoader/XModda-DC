# XModda Bot — Dashboard-connected AutoMod

This version reads each Discord server's AutoMod settings from the same Supabase `guild_settings` table used by the XModda dashboard.

## Render environment variables

Set these in Render (Production):

- `DISCORD_TOKEN` — your Discord bot token
- `SUPABASE_URL` — your Supabase project URL, e.g. `https://PROJECT_REF.supabase.co`
- `SUPABASE_SERVICE_ROLE_KEY` — your Supabase server-side secret key (`sb_secret_...`)

Do not commit secrets to GitHub.

## Required Discord bot settings/permissions

- Message Content Intent: ON in Discord Developer Portal
- Server Members Intent: ON for raid protection
- Bot permission: Manage Messages (delete AutoMod messages)
- Bot permission: Moderate Members (timeouts after repeated violations)
- Bot permission: View Channel / Send Messages (warnings/logs)

## Dashboard connection

The bot polls Supabase per guild with a short cache. When the dashboard saves a guild's settings, the bot picks them up automatically (normally within about 15 seconds).

Supported dashboard switches:

- Anti-Spam
- Anti-Links
- Anti-Invites
- Bad Words
- Excessive Caps
- Duplicate Messages
- Raid Protection

The numeric AutoMod settings in the dashboard are also read by the bot.
