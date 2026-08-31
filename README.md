# XModda Full Bot

Deploy `main.py` and `requirements.txt` to Render.

Environment variables:
- DISCORD_TOKEN
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- PORT (optional)

Start command: `python main.py`

Required Discord intents:
- Message Content Intent ON
- Server Members Intent ON

Required bot permissions depend on enabled features; for the full build use View Channels, Send Messages, Read Message History, Manage Messages, Moderate Members, Manage Channels, Manage Roles, Kick Members, and Ban Members.

Diagnostics: `/xmodda_diag`
AutoMod status: `/automod_status`
Reload settings: `/automod_reload`
