"""Polling engine: one BotRunner per bot (main + every clone), managed by BotManager.

No webhooks: each runner long-polls getUpdates, so no public URL / HTTPS is needed.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .reactions import react_to_update
from .tg import TelegramAPI, TelegramError

log = logging.getLogger("engine")

SETTINGS_TTL = 30  # seconds a clone's settings are cached before re-reading MongoDB
MAIN_UPDATES = ["message", "channel_post", "callback_query"]
CLONE_UPDATES = ["message", "channel_post"]


class BotRunner:
    def __init__(self, manager: "BotManager", kind: str, token: str, bot_id: int, username: str):
        self.manager = manager
        self.kind = kind  # "main" | "clone"
        self.bot_id = bot_id
        self.username = username
        self.api = TelegramAPI(token, manager.http)
        self._task: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        self._settings: dict | None = None
        self._settings_at = 0.0

    # ── lifecycle ──
    def start(self, drop_pending: bool = False) -> None:
        self._task = asyncio.create_task(self._poll(drop_pending), name=f"poll-{self.username}")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._handlers) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._handlers.clear()

    # ── clone settings (always sourced from MongoDB, cached briefly) ──
    async def get_settings(self) -> dict:
        if self._settings is None or time.monotonic() - self._settings_at > SETTINGS_TTL:
            doc = await self.manager.db.get_clone(self.bot_id)
            if doc:
                self._settings = doc.get("settings") or {}
                self._settings_at = time.monotonic()
        cfg = self.manager.cfg
        s = self._settings or {}
        return {
            "custom_reactions": s.get("custom_reactions") or list(cfg.default_reactions),
            "reaction_delay": s.get("reaction_delay", cfg.default_delay),
        }

    def invalidate_settings(self) -> None:
        self._settings = None

    # ── polling loop ──
    async def _poll(self, drop_pending: bool) -> None:
        try:
            await self.api.delete_webhook(drop_pending)  # webhook and getUpdates can't coexist
        except TelegramError as e:
            if e.code == 401:
                return await self.manager.on_token_dead(self)
            log.warning("@%s deleteWebhook failed: %s", self.username, e.description)

        allowed = MAIN_UPDATES if self.kind == "main" else CLONE_UPDATES
        offset, backoff = 0, 1
        log.info("Polling started: @%s (%s)", self.username, self.kind)

        while True:
            try:
                updates = await self.api.get_updates(offset, 30, allowed)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code == 401:
                    return await self.manager.on_token_dead(self)
                if e.code == 409:  # same token polled elsewhere / webhook set
                    log.warning("@%s: 409 conflict — is another instance running this token?", self.username)
                    await asyncio.sleep(10)
                elif e.code == 429:
                    await asyncio.sleep((e.retry_after or 5) + 1)
                else:
                    log.warning("@%s getUpdates error: %s", self.username, e.description)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                t = asyncio.create_task(self._dispatch(update))  # slow handlers/delays never block polling
                self._handlers.add(t)
                t.add_done_callback(self._handlers.discard)

    async def _dispatch(self, update: dict) -> None:
        try:
            if self.kind == "main":
                await self.manager.main_bot.handle(self, update)
            else:
                await self.manager.handle_clone_update(self, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error while processing update for @%s", self.username)


class BotManager:
    def __init__(self, cfg, db, http, stop_event: asyncio.Event | None = None):
        from .main_bot import MainBot  # local import: main_bot depends on this module's types

        self.cfg = cfg
        self.db = db
        self.http = http
        self.stop_event = stop_event or asyncio.Event()
        self.runners: dict[int, BotRunner] = {}
        self.main_runner: BotRunner | None = None
        self.main_bot = MainBot(cfg, db, self)

    @property
    def main_username(self) -> str:
        return self.main_runner.username if self.main_runner else ""

    async def start(self) -> None:
        api = TelegramAPI(self.cfg.bot_token, self.http)
        try:
            me = await api.get_me()
        except TelegramError as e:
            raise RuntimeError(f"BOT_TOKEN rejected by Telegram: {e.description}") from None

        self.main_runner = BotRunner(self, "main", self.cfg.bot_token, me["id"], me["username"])
        self.runners[me["id"]] = self.main_runner
        self.main_runner.start()

        count = 0
        for doc in await self.db.get_all_clones():
            if doc["bot_id"] == me["id"] or doc["bot_id"] in self.runners:
                continue
            self._spawn_clone(doc)
            count += 1
        log.info("Main bot @%s running with %d clone(s) loaded from MongoDB", me["username"], count)

    def _spawn_clone(self, doc: dict, drop_pending: bool = False) -> BotRunner:
        runner = BotRunner(self, "clone", doc["bot_token"], doc["bot_id"], doc["bot_username"])
        self.runners[doc["bot_id"]] = runner
        runner.start(drop_pending)
        return runner

    async def add_clone(self, doc: dict) -> None:
        # drop_pending: a brand-new clone shouldn't react to old backlog
        self._spawn_clone(doc, drop_pending=True)

    async def remove_clone(self, bot_id: int) -> None:
        runner = self.runners.pop(bot_id, None)
        if runner and runner is not self.main_runner:
            await runner.stop()
        await self.db.delete_clone(bot_id)

    def invalidate_settings(self, bot_id: int) -> None:
        runner = self.runners.get(bot_id)
        if runner:
            runner.invalidate_settings()

    async def handle_clone_update(self, runner: BotRunner, update: dict) -> None:
        msg = update.get("message")
        if msg and msg["chat"]["type"] == "private" and (msg.get("text") or "").startswith("/start"):
            main = f"@{self.main_username}" if self.main_username else "the main bot"
            await runner.api.send_message(
                msg["chat"]["id"],
                "👋 <b>Auto Reaction Bot</b>\n\nAdd me to your group or channel and I'll react to "
                f"every message.\n\n⚙️ Reactions and delay are managed from {main}.",
            )
            return
        s = await runner.get_settings()
        await react_to_update(runner.api, update, s["custom_reactions"], s["reaction_delay"])

    async def on_token_dead(self, runner: BotRunner) -> None:
        if runner.kind == "main":
            log.critical("Main BOT_TOKEN was rejected (401). Shutting down.")
            self.stop_event.set()
            return
        log.warning("Clone @%s: token revoked (401) — polling stopped", runner.username)
        doc = await self.db.get_clone(runner.bot_id)
        self.runners.pop(runner.bot_id, None)
        if doc and self.main_runner:
            try:
                await self.main_runner.api.send_message(
                    doc["user_id"],
                    f"⚠️ The token of <b>@{runner.username}</b> was revoked, so it stopped working.\n"
                    "Delete it in ⚙️ Settings and add it again with the new token.",
                )
            except TelegramError:
                pass

    async def stop_all(self) -> None:
        await asyncio.gather(*(r.stop() for r in list(self.runners.values())), return_exceptions=True)
        self.runners.clear()
