# 🤖 Auto Reaction Bot — v4 (Python · MongoDB · polling)

Telegram bot that reacts to every message in private chats, groups and channels.
One **main bot** (`BOT_TOKEN`) plus **clone bots** that users create themselves and that are stored in **MongoDB**.
Runs by **long polling** — no webhook, no domain, no HTTPS. Works on any always-on server (VPS / Docker / Railway worker).

## How it works
```
main.py ─► BotManager ─┬─ BotRunner(main)        getUpdates loop  → MainBot (UI, /clone, settings)
                       ├─ BotRunner(clone A)     getUpdates loop  → reacts using DB settings
                       └─ BotRunner(clone B) …   (loaded from MongoDB on startup, added/removed live)
```
- Every bot has its own long-polling loop; each update is handled in its own task, so a delay never blocks polling.
- A clone's reaction = random pick from `settings.custom_reactions`, preceded by `await asyncio.sleep(settings.reaction_delay)`.
- Settings are read from MongoDB (cached 30 s, cache is invalidated instantly when you change a setting in the menu).

## Features
- ⚙️ **Settings** button on `/start` → your clones (`@bot_username`) → control panel:
  🎭 Custom Reactions (view / edit / reset) · ⏱️ Reaction Time (0s · 2s · 5s · 10s · Custom) · 🗑️ Delete Clone (with confirmation) · 🔙 Back
- ➕ Add clone with `/clone <token>` or the **Add Clone** button. The token is verified with `getMe` first, and your message containing it is deleted.
- Limits: **5 clones** per normal user; **unlimited** for `OWNER_ID` and `ADMIN_IDS`.
- Clone tokens revoked in BotFather are detected (401), polling stops and the owner is notified.
- Commands: `/start /help /settings /clone /ping /stats` · admin: `/broadcast` (reply to a message)

## MongoDB schema — `clone_bots`
```json
{
  "user_id": 12345678,
  "bot_token": "123456:ABC...",
  "bot_id": 987654321,
  "bot_username": "MyCloneBot",
  "created_at": "<datetime, UTC>",
  "settings": { "custom_reactions": ["👍", "❤", "🔥"], "reaction_delay": 5 }
}
```
Indexes: `bot_id` (unique), `user_id`. A second collection, `users`, stores user IDs for `/stats` and `/broadcast`.

## Run with Docker (recommended)
```bash
cp .env.example .env     # set BOT_TOKEN, OWNER_ID  (MONGO_URI default works with the bundled mongo)
docker compose up -d --build
docker compose logs -f bot
```
## Run without Docker
```bash
pip install -r requirements.txt
# set MONGO_URI to your MongoDB / Atlas URL in .env
python main.py
```
## Configuration
| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Main bot token (single token — `BOT_TOKENS` was removed) |
| `OWNER_ID` | ✅ | Your Telegram ID: unlimited clones, `/broadcast` |
| `MONGO_URI` | ✅ | MongoDB connection string |
| `DB_NAME` | | default `autoreactionbot` |
| `EMOJI_LIST` | | Main bot's reactions + defaults for new clones |
| `ADMIN_IDS` | | Comma-separated extra admins |
| `MAX_CLONES` | | default `5` |
| `DEFAULT_DELAY` / `MAX_DELAY` | | default `0` / `300` seconds |

## Notes
- **Clone tokens are stored in plain text in MongoDB (as per the schema).** Keep MongoDB private (the compose file doesn't publish its port), use auth on Atlas/self-hosted, and back it up.
- Never run the same token in two places (Telegram returns 409 Conflict).
- Channels: add the bot as **admin**. Groups: bots only see all messages if they are admins or privacy mode is off.
- Only emoji from Telegram's allowed reaction list work; the editor validates this for you.
- Removed vs. v3 (Node.js): webhook mode, Vercel/Cloudflare deploy, `BOT_TOKENS`, welcome/goodbye, force-subscribe, per-chat random level, restrict/pause commands, landing page, ads footer.

## Tests
```bash
python -m unittest discover -s tests -v   # in-memory fakes, no MongoDB/network needed
```
