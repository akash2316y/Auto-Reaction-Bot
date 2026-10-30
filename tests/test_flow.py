"""Logic tests with in-memory fakes (no MongoDB / network needed).  Run: python -m unittest -v"""
import asyncio
import os
import sys
import time
import types
import unittest
from datetime import datetime, timezone

# Let the tests run even if httpx/motor aren't installed.
for name, attrs in {
    "httpx": {"AsyncClient": object, "Limits": object, "Timeout": object, "HTTPError": Exception},
    "motor": {}, "motor.motor_asyncio": {"AsyncIOMotorClient": object},
    "dotenv": {"load_dotenv": lambda *a, **k: None},
}.items():
    try:
        __import__(name)
    except ImportError:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Config  # noqa: E402
from bot.main_bot import MainBot, parse_command  # noqa: E402
from bot.reactions import parse_reactions, react_to_update  # noqa: E402
from bot.tg import TelegramError  # noqa: E402

OWNER, USER = 1, 2
MAIN_TOKEN = "111111:MAINTOKENMAINTOKENMAINTOKEN"


class FakeDB:
    def __init__(self):
        self.clones, self.users = {}, set()

    async def add_clone(self, user_id, token, bot_id, username, reactions, delay):
        doc = {"user_id": user_id, "bot_token": token, "bot_id": bot_id, "bot_username": username,
               "created_at": datetime.now(timezone.utc),
               "settings": {"custom_reactions": list(reactions), "reaction_delay": delay}}
        self.clones[bot_id] = doc
        return doc

    async def get_clone(self, bot_id): return self.clones.get(bot_id)
    async def get_user_clones(self, uid): return [c for c in self.clones.values() if c["user_id"] == uid]
    async def count_user_clones(self, uid): return len(await self.get_user_clones(uid))
    async def count_clones(self): return len(self.clones)
    async def set_clone_setting(self, bot_id, key, value): self.clones[bot_id]["settings"][key] = value
    async def delete_clone(self, bot_id): self.clones.pop(bot_id, None)
    async def add_user(self, uid): self.users.add(uid)
    async def remove_user(self, uid): self.users.discard(uid)
    async def count_users(self): return len(self.users)
    async def all_user_ids(self): return list(self.users)


class FakeAPI:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        async def f(*a, **k):
            self.calls.append((name, a, k))
            if name == "send_message":
                return {"message_id": 99}
            return {}
        return f
    def last(self, name): return [c for c in self.calls if c[0] == name][-1]


class FakeManager:
    def __init__(self):
        self.http, self.runners, self.added, self.removed, self.invalidated = None, {}, [], [], []
    async def add_clone(self, doc): self.added.append(doc["bot_id"])
    async def remove_clone(self, bot_id):
        self.removed.append(bot_id); self.runners.pop(bot_id, None)
        await self.db.delete_clone(bot_id)
    def invalidate_settings(self, bot_id): self.invalidated.append(bot_id)


class Runner:
    def __init__(self): self.api, self.username, self.bot_id = FakeAPI(), "MainBot", 111


def make_cfg(**kw):
    base = dict(bot_token=MAIN_TOKEN, owner_id=OWNER, mongo_uri="x", db_name="x",
                default_reactions=("👍", "❤"), admin_ids=frozenset(), max_clones=5, default_delay=0, max_delay=300)
    base.update(kw)
    return Config(**base)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg, self.db, self.mgr, self.runner = make_cfg(), FakeDB(), FakeManager(), Runner()
        self.mgr.db = self.db
        self.bot = MainBot(self.cfg, self.db, self.mgr)
        self.next_bot = 1000

        async def fake_get_me(this):  # token verification stub
            if "BAD" in this._base:
                raise TelegramError("getMe", 401, "Unauthorized")
            self.next_bot += 1
            return {"id": self.next_bot, "is_bot": True, "username": f"Clone{self.next_bot}"}
        import bot.main_bot as mb
        self._orig = mb.TelegramAPI.get_me
        mb.TelegramAPI.get_me = fake_get_me
        self._orig_init = mb.TelegramAPI.__init__
        mb.TelegramAPI.__init__ = lambda s, token, client: setattr(s, "_base", token)

    async def asyncTearDown(self):
        import bot.main_bot as mb
        mb.TelegramAPI.get_me, mb.TelegramAPI.__init__ = self._orig, self._orig_init

    def token(self, n): return f"{200000 + n}:ABCDEFGHIJKLMNOPQRSTUVWXYZ{n}"

    def msg(self, uid, text, chat_type="private"):
        return {"message_id": 5, "chat": {"id": uid, "type": chat_type}, "from": {"id": uid, "first_name": "Tester"}, "text": text}

    def cb(self, uid, data):
        return {"callback_query": {"id": "c", "from": {"id": uid, "first_name": "T"}, "data": data,
                                   "message": {"message_id": 7, "chat": {"id": uid}}}}


class CloneRules(Base):
    async def test_normal_user_capped_at_5(self):
        for i in range(5):
            ok, _ = await self.bot.create_clone(self.runner, USER, self.token(i))
            self.assertTrue(ok)
        ok, text = await self.bot.create_clone(self.runner, USER, self.token(99))
        self.assertFalse(ok)
        self.assertIn("limit", text.lower())
        self.assertEqual(await self.db.count_user_clones(USER), 5)

    async def test_owner_unlimited(self):
        for i in range(8):
            ok, _ = await self.bot.create_clone(self.runner, OWNER, self.token(i))
            self.assertTrue(ok)
        self.assertEqual(await self.db.count_user_clones(OWNER), 8)

    async def test_invalid_token_rejected(self):
        ok, text = await self.bot.create_clone(self.runner, USER, "not a token")
        self.assertFalse(ok)
        ok, text = await self.bot.create_clone(self.runner, USER, "123456:BADBADBADBADBADBADBADBADBAD")
        self.assertFalse(ok)
        self.assertIn("Invalid", text)
        self.assertEqual(len(self.db.clones), 0)

    async def test_schema_and_defaults(self):
        ok, _ = await self.bot.create_clone(self.runner, USER, self.token(1))
        doc = next(iter(self.db.clones.values()))
        self.assertEqual(set(doc), {"user_id", "bot_token", "bot_id", "bot_username", "created_at", "settings"})
        self.assertEqual(doc["settings"], {"custom_reactions": ["👍", "❤"], "reaction_delay": 0})
        self.assertEqual(self.mgr.added, [doc["bot_id"]])

    async def test_main_token_rejected(self):
        ok, _ = await self.bot.create_clone(self.runner, USER, MAIN_TOKEN)
        self.assertFalse(ok)


class SettingsMenu(Base):
    async def setup_clone(self):
        await self.bot.create_clone(self.runner, USER, self.token(1))
        return next(iter(self.db.clones))

    async def test_start_has_settings_button(self):
        await self.bot.handle(self.runner, {"message": self.msg(USER, "/start")})
        kb = self.runner.api.last("send_message")[1][2]
        self.assertIn("⚙️ Settings", [b["text"] for row in kb for b in row])

    async def test_list_panel_and_isolation(self):
        bid = await self.setup_clone()
        await self.bot.handle(self.runner, self.cb(USER, "sl:0"))
        kb = self.runner.api.last("edit_message_text")[1][3]
        self.assertIn(f"@Clone{bid}", [b["text"] for row in kb for b in row])
        await self.bot.handle(self.runner, self.cb(USER, f"cb:{bid}"))
        kb = self.runner.api.last("edit_message_text")[1][3]
        labels = [b["text"] for row in kb for b in row]
        for want in ("🎭 Custom Reactions", "⏱️ Reaction Time", "🗑️ Delete Clone", "🔙 Back"):
            self.assertIn(want, labels)
        # another user can't open / delete it
        await self.bot.handle(self.runner, self.cb(999, f"delc:{bid}"))
        self.assertIn(bid, self.db.clones)

    async def test_delay_presets_and_custom(self):
        bid = await self.setup_clone()
        await self.bot.handle(self.runner, self.cb(USER, f"dls:{bid}:5"))
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 5)
        await self.bot.handle(self.runner, self.cb(USER, f"dlc:{bid}"))
        await self.bot.handle(self.runner, {"message": self.msg(USER, "42")})
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 42)
        await self.bot.handle(self.runner, self.cb(USER, f"dlc:{bid}"))
        await self.bot.handle(self.runner, {"message": self.msg(USER, "99999")})
        self.assertEqual(self.db.clones[bid]["settings"]["reaction_delay"], 42)  # rejected
        self.assertIn(bid, self.mgr.invalidated)

    async def test_edit_reactions(self):
        bid = await self.setup_clone()
        await self.bot.handle(self.runner, self.cb(USER, f"rxe:{bid}"))
        await self.bot.handle(self.runner, {"message": self.msg(USER, "🎉 🔥❤️ 🤷‍♂️")})
        self.assertEqual(self.db.clones[bid]["settings"]["custom_reactions"], ["🎉", "🔥", "❤", "🤷‍♂"])
        await self.bot.handle(self.runner, self.cb(USER, f"rxe:{bid}"))
        await self.bot.handle(self.runner, {"message": self.msg(USER, "🎉 abc")})
        self.assertEqual(len(self.db.clones[bid]["settings"]["custom_reactions"]), 4)  # unchanged
        await self.bot.handle(self.runner, self.cb(USER, f"rxr:{bid}"))
        self.assertEqual(self.db.clones[bid]["settings"]["custom_reactions"], ["👍", "❤"])

    async def test_delete_flow(self):
        bid = await self.setup_clone()
        await self.bot.handle(self.runner, self.cb(USER, f"del:{bid}"))
        self.assertIn(bid, self.db.clones)  # confirmation first
        await self.bot.handle(self.runner, self.cb(USER, f"delc:{bid}"))
        self.assertNotIn(bid, self.db.clones)
        self.assertEqual(self.mgr.removed, [bid])

    async def test_clone_command_flow_and_token_message_deleted(self):
        await self.bot.handle(self.runner, {"message": self.msg(USER, "/clone")})
        await self.bot.handle(self.runner, {"message": self.msg(USER, self.token(3))})
        self.assertEqual(await self.db.count_user_clones(USER), 1)
        self.assertEqual(self.runner.api.last("delete_message")[1], (USER, 5))


class ReactionDelay(unittest.IsolatedAsyncioTestCase):
    async def test_delay_then_react_from_list(self):
        api = FakeAPI()
        upd = {"channel_post": {"message_id": 3, "chat": {"id": -100, "type": "channel"}, "text": "hi"}}
        t0 = time.monotonic()
        ok = await react_to_update(api, upd, ["🔥", "🎉"], 0.3)
        self.assertTrue(ok)
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)
        self.assertIn(api.last("set_message_reaction")[1][2], ("🔥", "🎉"))

    async def test_service_message_skipped(self):
        api = FakeAPI()
        upd = {"message": {"message_id": 3, "chat": {"id": 1, "type": "group"}, "new_chat_members": [{}]}}
        self.assertFalse(await react_to_update(api, upd, ["🔥"], 0))
        self.assertEqual(api.calls, [])


class Helpers(unittest.TestCase):
    def test_parse_reactions(self):
        self.assertEqual(parse_reactions("👍❤️🔥")[0], ["👍", "❤", "🔥"])
        self.assertEqual(parse_reactions("❤‍🔥 ❤")[0], ["❤‍🔥", "❤"])
        self.assertEqual(parse_reactions("👍 x 🦖")[1], ["x", "🦖"])

    def test_parse_command(self):
        self.assertEqual(parse_command("/start@MainBot hi", "MainBot"), ("start", "hi", True))
        self.assertFalse(parse_command("/start@Other", "MainBot")[2])
        self.assertEqual(parse_command("hello", "MainBot")[0], None)


class EngineLoop(unittest.IsolatedAsyncioTestCase):
    async def test_clone_polls_and_reacts_with_db_settings_and_delay(self):
        from bot.engine import BotManager, BotRunner
        db = FakeDB()
        await db.add_clone(USER, "t", 555, "CloneX", ["🎉"], 0)
        await db.set_clone_setting(555, "reaction_delay", 0.2)

        mgr = BotManager(make_cfg(), db, None)
        runner = BotRunner(mgr, "clone", "t", 555, "CloneX")
        batches = [[{"update_id": 10, "channel_post": {"message_id": 1, "chat": {"id": -1, "type": "channel"}, "text": "a"}}]]
        reacted = []

        class API(FakeAPI):
            async def delete_webhook(self, drop=False): self.dropped = drop
            async def get_updates(self, offset, timeout, allowed):
                self.offset_seen = offset
                if batches:
                    return batches.pop(0)
                await asyncio.sleep(10)
            async def set_message_reaction(self, chat_id, mid, emoji): reacted.append((time.monotonic(), emoji))
        runner.api = API()

        t0 = time.monotonic()
        runner.start()
        await asyncio.sleep(0.5)
        await runner.stop()
        self.assertEqual([e for _, e in reacted], ["🎉"])
        self.assertGreaterEqual(reacted[0][0] - t0, 0.19)   # reaction_delay honoured
        self.assertEqual(runner.api.offset_seen, 11)         # offset advanced

    async def test_settings_change_is_picked_up_after_invalidate(self):
        from bot.engine import BotManager, BotRunner
        db = FakeDB()
        await db.add_clone(USER, "t", 555, "CloneX", ["🎉"], 0)
        runner = BotRunner(BotManager(make_cfg(), db, None), "clone", "t", 555, "CloneX")
        self.assertEqual((await runner.get_settings())["custom_reactions"], ["🎉"])
        await db.set_clone_setting(555, "custom_reactions", ["🔥"])
        runner.invalidate_settings()
        self.assertEqual((await runner.get_settings())["custom_reactions"], ["🔥"])


if __name__ == "__main__":
    unittest.main()
