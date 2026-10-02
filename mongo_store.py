"""MongoDB persistence for the bot's DB dict.

The bot keeps one in-memory dict (DB). This module loads it from MongoDB at
startup and writes back only what changed:
  collection `users`  -> one document per user   (_id = user id string)
  collection `meta`   -> one document per other top-level key (_id = key)
"""
import hashlib
import json
import logging

from pymongo import MongoClient, ReplaceOne

logger = logging.getLogger(__name__)


class MongoStore:
    def __init__(self, uri: str, db_name: str = "ytbot"):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        self.client.admin.command("ping")  # fail fast if URI is wrong
        self.db = self.client[db_name]
        self.users = self.db["users"]
        self.meta = self.db["meta"]
        self._hashes: dict = {}  # "u:<id>" / "m:<key>" -> hash of last saved value

    @staticmethod
    def _h(value) -> str:
        return hashlib.md5(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def is_empty(self) -> bool:
        return self.users.estimated_document_count() == 0 and self.meta.estimated_document_count() == 0

    def load(self, defaults: dict) -> dict:
        data = dict(defaults)
        data["users"] = {}
        for doc in self.users.find():
            uid = doc.pop("_id")
            data["users"][uid] = doc
            self._hashes["u:" + uid] = self._h(doc)
        for doc in self.meta.find():
            key = doc["_id"]
            data[key] = doc.get("value")
            self._hashes["m:" + key] = self._h(data[key])
        return data

    def save(self, data: dict) -> None:
        ops = []
        for uid, u in data.get("users", {}).items():
            h = self._h(u)
            if self._hashes.get("u:" + uid) != h:
                ops.append(ReplaceOne({"_id": uid}, {"_id": uid, **u}, upsert=True))
                self._hashes["u:" + uid] = h
        if ops:
            self.users.bulk_write(ops, ordered=False)
        mops = []
        for key, val in data.items():
            if key == "users":
                continue
            h = self._h(val)
            if self._hashes.get("m:" + key) != h:
                mops.append(ReplaceOne({"_id": key}, {"_id": key, "value": val}, upsert=True))
                self._hashes["m:" + key] = h
        if mops:
            self.meta.bulk_write(mops, ordered=False)
