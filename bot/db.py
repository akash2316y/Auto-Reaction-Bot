"""MongoDB access layer (Motor). Collections: clone_bots, users."""
from __future__ import annotations

from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient


class Database:
    def __init__(self, uri: str, name: str):
        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10_000)
        db = self._client[name]
        self.clones = db["clone_bots"]
        self.users = db["users"]

    async def init(self) -> None:
        await self._client.admin.command("ping")  # fail fast if Mongo is unreachable
        await self.clones.create_index("bot_id", unique=True)
        await self.clones.create_index("user_id")

    def close(self) -> None:
        self._client.close()

    # ── clone_bots ──
    async def add_clone(self, user_id: int, token: str, bot_id: int, username: str,
                        reactions: list[str], delay: int) -> dict:
        doc = {
            "user_id": user_id,
            "bot_token": token,
            "bot_id": bot_id,
            "bot_username": username,
            "created_at": datetime.now(timezone.utc),
            "settings": {"custom_reactions": list(reactions), "reaction_delay": delay},
        }
        await self.clones.insert_one(doc)
        return doc

    async def get_clone(self, bot_id: int) -> dict | None:
        return await self.clones.find_one({"bot_id": bot_id})

    async def get_user_clones(self, user_id: int) -> list[dict]:
        return await self.clones.find({"user_id": user_id}).sort("created_at", 1).to_list(length=None)

    async def count_user_clones(self, user_id: int) -> int:
        return await self.clones.count_documents({"user_id": user_id})

    async def get_all_clones(self) -> list[dict]:
        return await self.clones.find({}).to_list(length=None)

    async def count_clones(self) -> int:
        return await self.clones.count_documents({})

    async def set_clone_setting(self, bot_id: int, key: str, value) -> None:
        await self.clones.update_one({"bot_id": bot_id}, {"$set": {f"settings.{key}": value}})

    async def delete_clone(self, bot_id: int) -> None:
        await self.clones.delete_one({"bot_id": bot_id})

    # ── users (for /stats and /broadcast) ──
    async def add_user(self, user_id: int) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "joined_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def remove_user(self, user_id: int) -> None:
        await self.users.delete_one({"user_id": user_id})

    async def count_users(self) -> int:
        return await self.users.count_documents({})

    async def all_user_ids(self) -> list[int]:
        return [d["user_id"] async for d in self.users.find({}, {"user_id": 1})]
