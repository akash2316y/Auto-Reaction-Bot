"""Main bot: /start, ⚙️ Settings menu, clone creation, owner tools."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from .reactions import MAX_REACTIONS, parse_reactions, react_to_update
from .tg import TelegramAPI, TelegramError

log = logging.getLogger("main_bot")

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,}$")
PENDING_TTL = 600  # seconds a "send me X" prompt stays active
PAGE_SIZE = 8
DELAY_PRESETS = (0, 2, 5, 10)


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _url(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def parse_command(text: str, my_username: str) -> tuple[str | None, str, bool]:
    """-> (command, args, addressed_to_me). command is None when text isn't a command."""
    if not text.startswith("/"):
        return None, "", True
    head, _, args = text.partition(" ")
    cmd, _, target = head[1:].partition("@")
    mine = not target or target.lower() == my_username.lower()
    return cmd.lower(), args.strip(), mine


def fmt_delay(seconds: int) -> str:
    return "instant" if not seconds else f"{seconds}s"


class MainBot:
    def __init__(self, cfg, db, manager):
        self.cfg = cfg
        self.db = db
        self.manager = manager
        self.pending: dict[int, dict] = {}  # user_id -> {"kind", "bot_id", "ts"}

    # ══════════════ entry point ══════════════
    async def handle(self, runner, update: dict) -> None:
        if "callback_query" in update:
            await self.on_callback(runner, update["callback_query"])
            return
        msg = update.get("message")
        if msg and await self.on_message(runner, msg):
            return
        # Not a command / input for us → the main bot reacts like any other bot.
        await react_to_update(runner.api, update, self.cfg.default_reactions, 0)

    # ══════════════ messages ══════════════
    async def on_message(self, runner, msg: dict) -> bool:
        """Return True if the message was consumed (no reaction needed)."""
        api: TelegramAPI = runner.api
        chat, user = msg["chat"], msg.get("from") or {}
        uid, text = user.get("id"), msg.get("text") or ""
        private = chat["type"] == "private"
        cmd, args, mine = parse_command(text, runner.username)

        if cmd is not None:
            if not mine:
                return False
            if not private:  # in groups only the light commands work
                if cmd in ("start", "help", "ping"):
                    await self._group_reply(api, chat["id"], msg["message_id"], cmd, runner)
                    return True
                return False
            self.pending.pop(uid, None)  # a new command cancels any pending prompt
            return await self._private_command(runner, msg, cmd, args)

        if private and uid in self.pending:
            return await self._pending_input(runner, msg, text)
        return False

    async def _group_reply(self, api, chat_id, message_id, cmd, runner):
        if cmd == "ping":
            await api.send_message(chat_id, "🏓 Pong!", reply_to=message_id)
        else:
            await api.send_message(
                chat_id,
                "👋 I react to every message here automatically.\n"
                f"Make your own reaction bot: @{runner.username}",
                reply_to=message_id,
            )

    async def _private_command(self, runner, msg, cmd, args) -> bool:
        api, chat_id, user = runner.api, msg["chat"]["id"], msg.get("from") or {}
        uid = user["id"]

        if cmd == "start":
            await self.db.add_user(uid)
            name = html.escape(user.get("first_name") or "there")
            await api.send_message(chat_id, self.home_text(name), self.kb_home(runner.username))
        elif cmd == "help":
            await api.send_message(chat_id, self.help_text(), [[_btn("⚙️ Settings", "sl:0")]])
        elif cmd == "settings":
            text, kb = await self.view_list(uid, 0)
            await api.send_message(chat_id, text, kb)
        elif cmd == "clone":
            await self._cmd_clone(runner, msg, args)
        elif cmd == "ping":
            t0 = time.monotonic()
            sent = await api.send_message(chat_id, "🏓 Pong!")
            ms = int((time.monotonic() - t0) * 1000)
            await api.edit_message_text(chat_id, sent["message_id"], f"🏓 Pong! <code>{ms} ms</code>")
        elif cmd == "stats":
            users, clones = await self.db.count_users(), await self.db.count_clones()
            await api.send_message(
                chat_id,
                f"📊 <b>Stats</b>\n\n👥 Users: <b>{users}</b>\n🤖 Clone bots: <b>{clones}</b>\n"
                f"🟢 Running bots: <b>{len(self.manager.runners)}</b>",
            )
        elif cmd == "broadcast":
            await self._cmd_broadcast(runner, msg)
        else:
            return False
        return True

    # ══════════════ clone creation ══════════════
    async def create_clone(self, runner, user_id: int, token: str) -> tuple[bool, str]:
        token = token.strip()
        if not TOKEN_RE.match(token):
            return False, "❌ That doesn't look like a bot token.\nIt looks like <code>123456789:ABC-DEF...</code> — get it from @BotFather."
        if token == self.cfg.bot_token:
            return False, "❌ That is the main bot's own token."

        admin = self.cfg.is_admin(user_id)
        if not admin and await self.db.count_user_clones(user_id) >= self.cfg.max_clones:
            return False, f"🚫 Clone limit reached ({self.cfg.max_clones}). Delete one in ⚙️ Settings first."

        try:  # verification: the token must be valid and belong to a bot
            me = await TelegramAPI(token, self.manager.http).get_me()
        except TelegramError:
            return False, "❌ Invalid token — Telegram rejected it. Check it in @BotFather and try again."
        if not me.get("is_bot"):
            return False, "❌ That token doesn't belong to a bot."

        existing = await self.db.get_clone(me["id"])
        if existing or me["id"] == runner.bot_id:
            return False, f"⚠️ @{me['username']} is already added."

        doc = await self.db.add_clone(
            user_id, token, me["id"], me["username"],
            list(self.cfg.default_reactions), self.cfg.default_delay,
        )
        await self.manager.add_clone(doc)
        return True, (
            f"✅ <b>@{me['username']}</b> is live!\n\n"
            "Add it to your group/channel (admin in channels) and it will start reacting.\n"
            "Customise it in ⚙️ Settings."
        )

    async def _cmd_clone(self, runner, msg, args):
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        if not args:
            self.pending[uid] = {"kind": "add_clone", "ts": time.time()}
            await api.send_message(chat_id, self.add_clone_prompt(), [[_btn("🔙 Back", "sl:0")]])
            return
        await self._finish_clone(runner, msg, args)

    async def _finish_clone(self, runner, msg, token):
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        self.pending.pop(uid, None)
        ok, text = await self.create_clone(runner, uid, token)
        try:  # don't leave the token sitting in the chat
            await api.delete_message(chat_id, msg["message_id"])
        except TelegramError:
            pass
        await api.send_message(chat_id, text, [[_btn("⚙️ Settings", "sl:0")]])

    # ══════════════ pending text input ══════════════
    async def _pending_input(self, runner, msg, text) -> bool:
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        state = self.pending.get(uid)
        if not state or time.time() - state["ts"] > PENDING_TTL:
            self.pending.pop(uid, None)
            return False

        kind = state["kind"]
        if kind == "add_clone":
            await self._finish_clone(runner, msg, text)
            return True

        doc = await self._owned_doc(uid, state["bot_id"])
        if not doc:
            self.pending.pop(uid, None)
            await api.send_message(chat_id, "❌ That clone no longer exists.", [[_btn("⚙️ Settings", "sl:0")]])
            return True

        back = [[_btn("🔙 Back", f"rx:{doc['bot_id']}" if kind == "reactions" else f"dl:{doc['bot_id']}")]]

        if kind == "reactions":
            valid, invalid = parse_reactions(text)
            if invalid:
                await api.send_message(
                    chat_id,
                    f"❌ Telegram doesn't allow these as reactions: {' '.join(invalid)}\nSend the list again.",
                    back)
                return True
            if not valid:
                await api.send_message(chat_id, "❌ I couldn't find any emoji. Send e.g. <code>👍 ❤ 🔥</code>", back)
                return True
            valid = valid[:MAX_REACTIONS]
            await self.db.set_clone_setting(doc["bot_id"], "custom_reactions", valid)
            self.manager.invalidate_settings(doc["bot_id"])
            self.pending.pop(uid, None)
            await api.send_message(chat_id, f"✅ Reactions updated: {' '.join(valid)}", back)
        else:  # custom delay
            if not text.strip().isdigit() or int(text) > self.cfg.max_delay:
                await api.send_message(
                    chat_id, f"❌ Send a whole number between 0 and {self.cfg.max_delay} (seconds).", back)
                return True
            secs = int(text)
            await self.db.set_clone_setting(doc["bot_id"], "reaction_delay", secs)
            self.manager.invalidate_settings(doc["bot_id"])
            self.pending.pop(uid, None)
            await api.send_message(chat_id, f"✅ Reaction delay set to <b>{fmt_delay(secs)}</b>.", back)
        return True

    # ══════════════ callbacks ══════════════
    async def on_callback(self, runner, cq: dict) -> None:
        api = runner.api
        uid, data = cq["from"]["id"], cq.get("data") or ""
        msg = cq.get("message")
        if not msg:
            await api.answer_callback(cq["id"])
            return
        chat_id, mid = msg["chat"]["id"], msg["message_id"]
        parts = data.split(":")
        action = parts[0]
        alert: str | None = None

        try:
            if action == "home":
                self.pending.pop(uid, None)
                name = html.escape(cq["from"].get("first_name") or "there")
                await self._show(api, chat_id, mid, self.home_text(name), self.kb_home(runner.username))
            elif action == "help":
                await self._show(api, chat_id, mid, self.help_text(), [[_btn("🔙 Back", "home")]])
            elif action == "sl":  # settings → clone list
                self.pending.pop(uid, None)
                text, kb = await self.view_list(uid, int(parts[1]))
                await self._show(api, chat_id, mid, text, kb)
            elif action == "add":
                self.pending[uid] = {"kind": "add_clone", "ts": time.time()}
                await self._show(api, chat_id, mid, self.add_clone_prompt(), [[_btn("🔙 Back", "sl:0")]])
            elif action in ("cb", "rx", "rxe", "rxr", "dl", "dls", "dlc", "del", "delc"):
                doc = await self._owned_doc(uid, int(parts[1]))
                if not doc:
                    alert = "This clone no longer exists."
                    text, kb = await self.view_list(uid, 0)
                    await self._show(api, chat_id, mid, text, kb)
                else:
                    alert = await self._clone_action(api, uid, chat_id, mid, action, doc, parts)
            else:
                alert = "Unknown action."
        except TelegramError as e:
            log.warning("callback error: %s", e.description)
        except (ValueError, IndexError):
            alert = "Invalid request."
        finally:
            try:
                await api.answer_callback(cq["id"], alert or "", bool(alert))
            except TelegramError:
                pass

    async def _clone_action(self, api, uid, chat_id, mid, action, doc, parts) -> str | None:
        bot_id = doc["bot_id"]
        if action == "cb":
            self.pending.pop(uid, None)
            await self._show(api, chat_id, mid, self.panel_text(doc), self.kb_panel(bot_id))

        elif action == "rx":  # 🎭 Custom Reactions (view)
            self.pending.pop(uid, None)
            await self._show(api, chat_id, mid, self.reactions_text(doc), self.kb_reactions(bot_id))
        elif action == "rxe":  # edit → wait for the emoji list
            self.pending[uid] = {"kind": "reactions", "bot_id": bot_id, "ts": time.time()}
            await self._show(
                api, chat_id, mid,
                "✏️ <b>Send the new reaction list</b>\n\nExample: <code>👍 ❤ 🔥 🎉</code>\n"
                "Only Telegram's supported reaction emoji work. Max "
                f"{MAX_REACTIONS}.",
                [[_btn("🔙 Back", f"rx:{bot_id}")]])
        elif action == "rxr":  # reset to defaults
            await self.db.set_clone_setting(bot_id, "custom_reactions", list(self.cfg.default_reactions))
            self.manager.invalidate_settings(bot_id)
            doc = await self.db.get_clone(bot_id)
            await self._show(api, chat_id, mid, self.reactions_text(doc), self.kb_reactions(bot_id))
            return "Reset to default reactions."

        elif action == "dl":  # ⏱️ Reaction Time
            self.pending.pop(uid, None)
            await self._show(api, chat_id, mid, self.delay_text(doc), self.kb_delay(doc))
        elif action == "dls":  # preset
            secs = int(parts[2])
            if secs not in DELAY_PRESETS:
                return "Invalid value."
            await self.db.set_clone_setting(bot_id, "reaction_delay", secs)
            self.manager.invalidate_settings(bot_id)
            doc = await self.db.get_clone(bot_id)
            await self._show(api, chat_id, mid, self.delay_text(doc), self.kb_delay(doc))
            return f"Delay set to {fmt_delay(secs)}."
        elif action == "dlc":  # custom value
            self.pending[uid] = {"kind": "delay", "bot_id": bot_id, "ts": time.time()}
            await self._show(
                api, chat_id, mid,
                f"✏️ <b>Send the delay in seconds</b> (0–{self.cfg.max_delay}).\nExample: <code>7</code>",
                [[_btn("🔙 Back", f"dl:{bot_id}")]])

        elif action == "del":
            await self._show(
                api, chat_id, mid,
                f"🗑️ Delete <b>@{doc['bot_username']}</b>?\nIt will stop reacting immediately.",
                [[_btn("✅ Yes, delete", f"delc:{bot_id}"), _btn("❌ Cancel", f"cb:{bot_id}")]])
        elif action == "delc":
            await self.manager.remove_clone(bot_id)
            text, kb = await self.view_list(uid, 0)
            await self._show(api, chat_id, mid, text, kb)
            return f"@{doc['bot_username']} deleted."
        return None

    # ══════════════ views ══════════════
    async def _owned_doc(self, uid: int, bot_id: int) -> dict | None:
        doc = await self.db.get_clone(bot_id)
        return doc if doc and doc["user_id"] == uid else None

    async def _show(self, api, chat_id, mid, text, kb) -> None:
        try:
            await api.edit_message_text(chat_id, mid, text, kb)
        except TelegramError as e:
            if "not modified" not in e.description.lower():
                raise

    def home_text(self, name: str) -> str:
        return (
            f"👋 Hello <b>{name}</b>!\n\n"
            "I'm an <b>Auto Reaction Bot</b> — add me to a group or channel and I'll react to every message.\n\n"
            "🤖 Want your own reaction bot? Create a clone with /clone, then set its emoji and "
            "delay in ⚙️ Settings."
        )

    def help_text(self) -> str:
        return (
            "❓ <b>Help</b>\n\n"
            "/start — home\n/settings — manage your clone bots\n/clone — create a clone bot\n"
            "/ping — latency\n/stats — bot statistics\n\n"
            "<b>Make a clone</b>\n1. Create a bot in @BotFather and copy its token.\n"
            "2. Send /clone and paste the token.\n3. Add the clone to your group/channel (admin in channels).\n"
            "4. Tune it in ⚙️ Settings → your bot."
        )

    def add_clone_prompt(self) -> str:
        return (
            "➕ <b>Add a clone bot</b>\n\nSend the bot token from @BotFather "
            "(<code>123456789:ABC-DEF...</code>).\nI'll delete your message after reading it."
        )

    def kb_home(self, username: str) -> list:
        return [
            [_btn("⚙️ Settings", "sl:0")],
            [_url("➕ Add to Group", f"https://t.me/{username}?startgroup=true"),
             _url("➕ Add to Channel", f"https://t.me/{username}?startchannel=true")],
            [_btn("❓ Help", "help")],
        ]

    async def view_list(self, uid: int, page: int) -> tuple[str, list]:
        clones = await self.db.get_user_clones(uid)
        limit = "unlimited" if self.cfg.is_admin(uid) else str(self.cfg.max_clones)
        page = max(0, min(page, max(0, (len(clones) - 1) // PAGE_SIZE)))
        chunk = clones[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

        text = f"⚙️ <b>Settings</b>\n\nYour clone bots: <b>{len(clones)}</b> / {limit}\n"
        text += "\nChoose a bot to manage:" if clones else "\nYou have no clone bots yet. Tap ➕ Add Clone to create one."

        kb = [[_btn(f"@{c['bot_username']}", f"cb:{c['bot_id']}")] for c in chunk]
        nav = []
        if page > 0:
            nav.append(_btn("⬅️ Prev", f"sl:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(clones):
            nav.append(_btn("Next ➡️", f"sl:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append([_btn("➕ Add Clone", "add")])
        kb.append([_btn("🔙 Back", "home")])
        return text, kb

    def panel_text(self, doc: dict) -> str:
        s = doc.get("settings") or {}
        reactions = " ".join(s.get("custom_reactions") or []) or "—"
        created = doc["created_at"].strftime("%Y-%m-%d") if hasattr(doc.get("created_at"), "strftime") else "—"
        return (
            f"🤖 <b>@{doc['bot_username']}</b> — Control Panel\n\n"
            f"🎭 Reactions: {reactions}\n⏱️ Delay: <b>{fmt_delay(s.get('reaction_delay', 0))}</b>\n📅 Created: {created}"
        )

    def kb_panel(self, bot_id: int) -> list:
        return [
            [_btn("🎭 Custom Reactions", f"rx:{bot_id}")],
            [_btn("⏱️ Reaction Time", f"dl:{bot_id}")],
            [_btn("🗑️ Delete Clone", f"del:{bot_id}")],
            [_btn("🔙 Back", "sl:0")],
        ]

    def reactions_text(self, doc: dict) -> str:
        reactions = " ".join((doc.get("settings") or {}).get("custom_reactions") or []) or "—"
        return (f"🎭 <b>Custom Reactions</b> — @{doc['bot_username']}\n\nCurrent: {reactions}\n\n"
                "The bot picks one of these at random for every message.")

    def kb_reactions(self, bot_id: int) -> list:
        return [
            [_btn("✏️ Edit Reactions", f"rxe:{bot_id}")],
            [_btn("♻️ Reset to Default", f"rxr:{bot_id}")],
            [_btn("🔙 Back", f"cb:{bot_id}")],
        ]

    def delay_text(self, doc: dict) -> str:
        secs = (doc.get("settings") or {}).get("reaction_delay", 0)
        return (f"⏱️ <b>Reaction Time</b> — @{doc['bot_username']}\n\nCurrent delay: <b>{fmt_delay(secs)}</b>\n\n"
                "How long the bot waits before reacting to a message.")

    def kb_delay(self, doc: dict) -> list:
        bot_id = doc["bot_id"]
        cur = (doc.get("settings") or {}).get("reaction_delay", 0)
        presets = [_btn(("✅ " if s == cur else "") + f"{s}s", f"dls:{bot_id}:{s}") for s in DELAY_PRESETS]
        return [presets, [_btn("✏️ Custom", f"dlc:{bot_id}")], [_btn("🔙 Back", f"cb:{bot_id}")]]

    # ══════════════ owner tools ══════════════
    async def _cmd_broadcast(self, runner, msg) -> None:
        api, chat_id, uid = runner.api, msg["chat"]["id"], msg["from"]["id"]
        if not self.cfg.is_admin(uid):
            return
        target = msg.get("reply_to_message")
        if not target:
            await api.send_message(chat_id, "Reply to the message you want to broadcast with /broadcast.")
            return
        ids = await self.db.all_user_ids()
        await api.send_message(chat_id, f"📢 Broadcasting to {len(ids)} users…")
        ok = blocked = failed = 0
        for user_id in ids:
            try:
                await api.copy_message(user_id, chat_id, target["message_id"])
                ok += 1
            except TelegramError as e:
                if e.code in (400, 403):
                    blocked += 1
                    await self.db.remove_user(user_id)
                else:
                    failed += 1
            await asyncio.sleep(0.05)  # ~20 msg/s, under Telegram's limit
        await api.send_message(chat_id, f"✅ Done.\nSent: {ok}\nBlocked/removed: {blocked}\nFailed: {failed}")
