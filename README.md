# Game Rewards Bot

Telegram bot for game reward requests and events.

## Required Render environment variables
- BOT_TOKEN — a newly rotated Telegram bot token (do not reuse the token previously shared in chat).
- SUPABASE_URL — https://dvbjvxldmqtglwtsihbg.supabase.co
- SUPABASE_SERVICE_ROLE_KEY — Supabase server-side secret/service-role key. Never put this in GitHub or share it in chat.
- OWNER_ID — 9022804804

Build: `pip install -r requirements.txt`
Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`

The bot uses long polling. Reward delivery is manual: admins grant rewards in-game and then mark them delivered in the bot.
