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


### Member embeds
The dashboard's Welcome and Goodbye builders are rendered as real Discord embeds. `Thumbnail URL` is the small upper-right embed image, `Image URL` is the large bottom embed image, `Embed color` controls the embed accent, and `Footer text` uses Discord's actual embed footer.
