"""Authoritative cosmetic ledger. No client message can mint currency or entitlements.

PostgreSQL is the production storage option. SQLite is intentionally development-only
unless an operator explicitly attests that its directory is a persistent volume.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

SCHEMA_VERSION = 2
WELCOME_COINS = 150
REWARD_COINS = 50
REWARD_COOLDOWN = 4 * 60 * 60
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}\Z")
PRODUCTS = {"coins_500": 500, "coins_1500": 1500, "supporter_coffee": 0}
NAMES = {
    "ball": ["Rally Pearl", "Rosebud", "Tideglass", "Ember Egg", "Stardrop", "Sugar Comet", "Chronocore", "Moon Rabbit"],
    "paddle": ["Rally Pro", "Rose Parade", "Pocket Ocean", "Dragon's Rest", "Starliner", "Sugar Sprint", "Brassbound", "Moonbridge"],
    "brick": ["Prism Ceramic", "Mini Conservatory", "Mixtape", "Dragon Hoard", "Mooncake", "Geode"],
}
PRICES = {"ball": [200, 200, 200, 400, 400, 400, 600, 600], "paddle": [300, 300, 300, 500, 500, 500, 800, 800], "brick": [350, 350, 350, 600, 600, 600]}
CATALOG = [{"id": slot + "_classic", "slot": slot, "name": "Classic", "price": 0} for slot in ("ball", "paddle", "brick", "theme")]
for _slot, _names in NAMES.items():
    CATALOG.extend({"id": f"{_slot}_{i+1}", "slot": _slot, "name": name, "price": PRICES[_slot][i]} for i, name in enumerate(_names))
CATALOG.extend({"id": "theme_" + key, "slot": "theme", "name": name, "price": 1200} for key, name in (
    ("conservatory", "Midnight Conservatory"), ("observatory", "Tidal Observatory"),
    ("cloudline", "Cloudline Express"), ("arcade", "Celestial Arcade"), ("rally", "Rally Club")))
ITEMS = {item["id"]: item for item in CATALOG}
FREE_ITEMS = [item["id"] for item in CATALOG if item["price"] == 0]


class EconomyError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def operation_id(value):
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise EconomyError("bad_operation", "Use a stable operation identifier of 8 to 80 letters, digits, - or _")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Economy:
    def __init__(self, database: str, *, durable: bool = False, now=time.time):
        self.database, self.durable, self.now = database, durable, now
        self.postgres = database.startswith(("postgresql://", "postgres://"))
        self._lock = threading.RLock()
        self._in_transaction = False
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            self.db = psycopg.connect(database, row_factory=dict_row, autocommit=True, connect_timeout=10)
        else:
            if database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(database, isolation_level=None, check_same_thread=False, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        self._schema()

    @classmethod
    def from_environment(cls):
        url = os.environ.get("PB_DATABASE_URL", "")
        if url:
            if not url.startswith(("postgresql://", "postgres://")):
                raise ValueError("PB_DATABASE_URL must use PostgreSQL")
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query))
            if query.get("sslmode", "require") not in ("require", "verify-ca", "verify-full"):
                raise ValueError("Production PostgreSQL connections require TLS")
            query.setdefault("sslmode", "require")
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
            # This attestation is deliberate: a DB connection is not a backup policy.
            return cls(url, durable=os.environ.get("PB_DURABLE_STORAGE", "0") == "1")
        path = os.environ.get("PB_SQLITE_PATH", "")
        if not path:
            return None
        persistent = os.environ.get("PB_SQLITE_PERSISTENT_VOLUME", "0") == "1"
        if os.environ.get("RENDER") and not persistent:
            raise ValueError("Render's ephemeral filesystem cannot hold player wallets; configure PostgreSQL")
        return cls(path, durable=persistent)

    def close(self):
        with self._lock:
            self.db.close()

    def _sql(self, statement, parameters=()):
        if self.postgres and self.db.closed and not self._in_transaction:
            import psycopg
            from psycopg.rows import dict_row
            self.db = psycopg.connect(self.database, row_factory=dict_row, autocommit=True, connect_timeout=10)
        return self.db.execute(statement.replace("?", "%s") if self.postgres else statement, parameters)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._sql("BEGIN" if self.postgres else "BEGIN IMMEDIATE")
            self._in_transaction = True
            try:
                yield
                self._sql("COMMIT")
            except BaseException:
                if not self.postgres or not self.db.closed:
                    self._sql("ROLLBACK")
                raise
            finally:
                self._in_transaction = False

    def _schema(self):
        with self.transaction():
            for statement in [
                "CREATE TABLE IF NOT EXISTS pb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS pb_accounts (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created BIGINT NOT NULL, coins BIGINT NOT NULL CHECK(coins>=0), rating BIGINT NOT NULL, ranked_games BIGINT NOT NULL, wins BIGINT NOT NULL, supporter BIGINT NOT NULL DEFAULT 0, reward_at BIGINT NOT NULL DEFAULT 0, equipped TEXT NOT NULL, deleted BIGINT NOT NULL DEFAULT 0, store_review BIGINT NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS pb_owned (account_id TEXT NOT NULL REFERENCES pb_accounts(id), item_id TEXT NOT NULL, PRIMARY KEY(account_id,item_id))",
                "CREATE TABLE IF NOT EXISTS pb_operations (account_id TEXT NOT NULL REFERENCES pb_accounts(id), operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(account_id,operation_id))",
                "CREATE TABLE IF NOT EXISTS pb_ledger (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES pb_accounts(id), delta BIGINT NOT NULL, reason TEXT NOT NULL, created BIGINT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS pb_matches (id TEXT PRIMARY KEY, player0 TEXT NOT NULL REFERENCES pb_accounts(id), player1 TEXT NOT NULL REFERENCES pb_accounts(id), mode TEXT NOT NULL, seed BIGINT NOT NULL, created BIGINT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS pb_receipts (token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES pb_accounts(id), product_id TEXT NOT NULL, created BIGINT NOT NULL, amount BIGINT NOT NULL DEFAULT 0, remaining BIGINT NOT NULL DEFAULT 0, voided BIGINT NOT NULL DEFAULT 0, pending_token TEXT NOT NULL DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS pb_ad_challenges (token TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES pb_accounts(id), expires BIGINT NOT NULL, used BIGINT NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS pb_ad_transactions (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES pb_accounts(id), created BIGINT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS pb_match_pair_time ON pb_matches(player0,player1,created)",
            ]:
                self._sql(statement)
            row = self._sql("SELECT value FROM pb_meta WHERE key='schema'").fetchone()
            if row and int(row["value"]) not in (1, SCHEMA_VERSION):
                raise RuntimeError("Unsupported economy schema version; migrate before starting")
            additions = {"pb_accounts": {"deleted":"BIGINT NOT NULL DEFAULT 0", "store_review":"BIGINT NOT NULL DEFAULT 0"},
                         "pb_receipts": {"amount":"BIGINT NOT NULL DEFAULT 0", "remaining":"BIGINT NOT NULL DEFAULT 0", "voided":"BIGINT NOT NULL DEFAULT 0", "pending_token":"TEXT NOT NULL DEFAULT ''"}}
            for table, columns in additions.items():
                existing = ({item["column_name"] for item in self._sql("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchall()}
                            if self.postgres else {item["name"] for item in self._sql("PRAGMA table_info(" + table + ")").fetchall()})
                for column, definition in columns.items():
                    if column not in existing:
                        self._sql("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
            if not row:
                self._sql("INSERT INTO pb_meta(key,value) VALUES('schema',?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) < SCHEMA_VERSION:
                self._sql("UPDATE pb_accounts SET store_review=1 WHERE id IN (SELECT account_id FROM pb_receipts WHERE product_id<>'supporter_coffee' AND amount=0)")
                self._sql("UPDATE pb_meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))

    def _account(self, account_id, lock=False, include_deleted=False):
        row = self._sql("SELECT * FROM pb_accounts WHERE id=?" + (" FOR UPDATE" if lock and self.postgres else ""), (account_id,)).fetchone()
        if not row:
            raise EconomyError("account_missing", "Player account was not found")
        if row["deleted"] and not include_deleted:
            raise EconomyError("account_deleted", "This player account has been deleted")
        return dict(row)

    def authenticate(self, token=None):
        if token is not None:
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise EconomyError("invalid_token", "The saved player key is invalid; do not overwrite it")
            with self._lock:
                row = self._sql("SELECT id,deleted FROM pb_accounts WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                if not row:
                    raise EconomyError("invalid_token", "The saved player key was not recognized; do not overwrite it")
                if row["deleted"]:
                    raise EconomyError("account_deleted", "This player account has been deleted")
                return row["id"], None
        token = secrets.token_urlsafe(32)
        account_id = secrets.token_hex(16)
        now = int(self.now())
        equipped = {slot: slot + "_classic" for slot in ("ball", "paddle", "brick", "theme")}
        with self.transaction():
            self._sql("INSERT INTO pb_accounts(id,token_hash,created,coins,rating,ranked_games,wins,equipped) VALUES(?,?,?,?,?,?,?,?)",
                      (account_id, hashlib.sha256(token.encode()).hexdigest(), now, WELCOME_COINS, 1000, 0, 0, encode(equipped)))
            for item in FREE_ITEMS:
                self._sql("INSERT INTO pb_owned(account_id,item_id) VALUES(?,?)", (account_id, item))
            self._ledger(account_id, WELCOME_COINS, "welcome")
        return account_id, token

    def _ledger(self, account_id, amount, reason):
        self._sql("INSERT INTO pb_ledger(id,account_id,delta,reason,created) VALUES(?,?,?,?,?)", (secrets.token_hex(16), account_id, amount, reason, int(self.now())))

    def profile(self, account_id):
        with self._lock:
            a = self._account(account_id)
            owned = sorted(row["item_id"] for row in self._sql("SELECT item_id FROM pb_owned WHERE account_id=?", (account_id,)).fetchall())
            divisions = [(1800, "Diamond"), (1600, "Platinum"), (1400, "Gold"), (1200, "Silver"), (0, "Bronze")]
            recent = []
            for match in self._sql("SELECT player0,receipt FROM pb_matches WHERE (player0=? OR player1=?) AND state<>'pending' ORDER BY created DESC,id DESC LIMIT 10", (account_id, account_id)).fetchall():
                receipt = json.loads(match["receipt"])
                index = 0 if match["player0"] == account_id else 1
                receipt["coins"] = receipt.pop("awards")[index]
                receipt["rating_delta"] = receipt.pop("rating_deltas")[index]
                recent.append(receipt)
            return {"account_id": account_id, "billing_account_id": hashlib.sha256(account_id.encode()).hexdigest(),
                    "coins": a["coins"], "owned": owned, "equipped": json.loads(a["equipped"]), "supporter": bool(a["supporter"]), "recent_receipts": recent, "store_review": bool(a["store_review"]),
                    "ranked": {"rating": a["rating"], "games": a["ranked_games"], "wins": a["wins"],
                               "provisional": a["ranked_games"] < 10, "placements_remaining": max(0, 10-a["ranked_games"]),
                               "division": "Placement" if a["ranked_games"] < 10 else next(name for threshold, name in divisions if a["rating"] >= threshold)},
                    "reward": {"coins": REWARD_COINS, "cooldown_seconds": REWARD_COOLDOWN, "available_at": a["reward_at"], "server_time": int(self.now())}}

    def _prior(self, account_id, op, kind, data):
        operation_id(op)
        fingerprint = encode([kind, data])
        row = self._sql("SELECT * FROM pb_operations WHERE account_id=? AND operation_id=?", (account_id, op)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise EconomyError("operation_conflict", "This operation identifier already belongs to a different action")
            return fingerprint, json.loads(row["response"])
        return fingerprint, None

    def _remember(self, account_id, op, fingerprint, response):
        self._sql("INSERT INTO pb_operations(account_id,operation_id,fingerprint,response) VALUES(?,?,?,?)", (account_id, op, fingerprint, encode(response)))
        return response

    def buy(self, account_id, item_id, op):
        if not isinstance(item_id, str) or item_id not in ITEMS:
            raise EconomyError("unknown_item", "This cosmetic is not in the current catalog")
        with self.transaction():
            account = self._account(account_id, lock=True)
            fingerprint, prior = self._prior(account_id, op, "buy", item_id)
            if prior is not None:
                return prior
            owned = self._sql("SELECT 1 FROM pb_owned WHERE account_id=? AND item_id=?", (account_id, item_id)).fetchone()
            cost = 0 if owned else ITEMS[item_id]["price"]
            if account["coins"] < cost:
                raise EconomyError("insufficient_coins", "Keep playing to earn the remaining coins")
            if not owned:
                self._sql("UPDATE pb_accounts SET coins=coins-? WHERE id=?", (cost, account_id))
                # Spend paid balances first. This accounting lets refunds remove only
                # unspent paid coins without clawing back coins earned through play.
                paid_to_spend = cost
                for receipt in self._sql("SELECT token_hash,remaining FROM pb_receipts WHERE account_id=? AND voided=0 AND remaining>0 ORDER BY created,token_hash", (account_id,)).fetchall():
                    used = min(receipt["remaining"], paid_to_spend)
                    self._sql("UPDATE pb_receipts SET remaining=remaining-? WHERE token_hash=?", (used, receipt["token_hash"]))
                    paid_to_spend -= used
                    if paid_to_spend == 0:
                        break
                self._sql("INSERT INTO pb_owned(account_id,item_id) VALUES(?,?)", (account_id, item_id))
                self._ledger(account_id, -cost, "cosmetic:" + item_id)
            return self._remember(account_id, op, fingerprint, {"item_id": item_id, "spent": cost, "already_owned": bool(owned)})

    def equip(self, account_id, slot, item_id, op):
        if not isinstance(item_id, str) or item_id not in ITEMS or ITEMS[item_id]["slot"] != slot:
            raise EconomyError("wrong_slot", "This cosmetic does not belong in that slot")
        with self.transaction():
            account = self._account(account_id, lock=True)
            fingerprint, prior = self._prior(account_id, op, "equip", [slot, item_id])
            if prior is not None:
                return prior
            if not self._sql("SELECT 1 FROM pb_owned WHERE account_id=? AND item_id=?", (account_id, item_id)).fetchone():
                raise EconomyError("not_owned", "Unlock this cosmetic before equipping it")
            equipped = json.loads(account["equipped"])
            equipped[slot] = item_id
            self._sql("UPDATE pb_accounts SET equipped=? WHERE id=?", (encode(equipped), account_id))
            return self._remember(account_id, op, fingerprint, {"equipped": equipped})

    def prepare_ad(self, account_id, op):
        with self.transaction():
            account = self._account(account_id, lock=True)
            fingerprint, prior = self._prior(account_id, op, "prepare_ad", None)
            if prior is not None:
                return prior
            if account["reward_at"] > self.now():
                raise EconomyError("reward_cooldown", "Your next reward is still cooling down")
            token = secrets.token_urlsafe(32)
            expires = int(self.now()) + 600
            self._sql("INSERT INTO pb_ad_challenges(token,account_id,expires) VALUES(?,?,?)", (token, account_id, expires))
            return self._remember(account_id, op, fingerprint, {"custom_data": token, "ssv_user_id": account_id, "expires_at": expires, "coins": REWARD_COINS})

    def claim_supporter(self, account_id, op):
        with self.transaction():
            account = self._account(account_id, lock=True)
            fingerprint, prior = self._prior(account_id, op, "supporter_reward", None)
            if prior is not None:
                return prior
            if not account["supporter"]:
                raise EconomyError("supporter_required", "This reward needs a verified supporter purchase or a completed rewarded ad")
            self._grant_cooldown(account)
            return self._remember(account_id, op, fingerprint, {"coins": REWARD_COINS, "available_at": int(self.now()) + REWARD_COOLDOWN})

    def _grant_cooldown(self, account):
        if account["reward_at"] > self.now():
            raise EconomyError("reward_cooldown", "Your next reward is still cooling down")
        self._sql("UPDATE pb_accounts SET coins=coins+?,reward_at=? WHERE id=?", (REWARD_COINS, int(self.now())+REWARD_COOLDOWN, account["id"]))
        self._ledger(account["id"], REWARD_COINS, "optional_reward")

    def apply_ad_verified(self, *, transaction_id, user_id, custom_data, reward_amount, timestamp=None, **_):
        """Call only after StoreVerifier authenticates the ORIGINAL Google query."""
        if reward_amount != REWARD_COINS or not isinstance(transaction_id, str) or not 1 <= len(transaction_id) <= 256:
            raise EconomyError("invalid_receipt", "Unexpected verified ad reward")
        with self.transaction():
            account = self._account(user_id, lock=True)
            seen = self._sql("SELECT account_id FROM pb_ad_transactions WHERE id=?", (transaction_id,)).fetchone()
            if seen:
                if seen["account_id"] != user_id:
                    raise EconomyError("receipt_conflict", "This reward receipt belongs to another account")
                return {"duplicate": True, "coins": 0}
            challenge = self._sql("SELECT * FROM pb_ad_challenges WHERE token=?", (custom_data,)).fetchone()
            # Signed completion time, not delayed network delivery time, binds the challenge.
            if not challenge or challenge["account_id"] != user_id or challenge["used"] or type(timestamp) is not int or not (challenge["expires"] - 600) * 1000 <= timestamp <= challenge["expires"] * 1000:
                raise EconomyError("challenge_expired", "This rewarded ad authorization has expired or was used")
            self._grant_cooldown(account)
            self._sql("UPDATE pb_ad_challenges SET used=1 WHERE token=?", (custom_data,))
            self._sql("INSERT INTO pb_ad_transactions(id,account_id,created) VALUES(?,?,?)", (transaction_id, user_id, int(self.now())))
            return {"duplicate": False, "coins": REWARD_COINS}

    def apply_purchase_verified(self, account_id, *, product_id, token_hash, quantity=1, consumed=False, encrypted_token="", **_):
        """Call only with a Google-authenticated, account-bound purchased entitlement."""
        if product_id not in PRODUCTS or type(quantity) is not int or quantity != 1 or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
            raise EconomyError("invalid_receipt", "Unexpected verified purchase")
        with self.transaction():
            account = self._account(account_id, lock=True)
            seen = self._sql("SELECT * FROM pb_receipts WHERE token_hash=?", (token_hash,)).fetchone()
            if seen:
                if seen["account_id"] != account_id or seen["product_id"] != product_id:
                    raise EconomyError("receipt_conflict", "This purchase belongs to another account")
                if seen["voided"]:
                    raise EconomyError("receipt_voided", "Google Play has refunded or revoked this purchase")
                return {"duplicate": True, "coins": 0, "product_id": product_id}
            if account["store_review"]:
                raise EconomyError("store_review", "Paid purchases are on hold for a refunded purchase review; gameplay and earned cosmetics remain available")
            if consumed and product_id != "supporter_coffee":
                raise EconomyError("receipt_consumed", "This consumed purchase has no matching grant in this account ledger")
            amount = PRODUCTS[product_id]
            self._sql("UPDATE pb_accounts SET coins=coins+? WHERE id=?", (amount, account_id))
            if product_id == "supporter_coffee":
                self._sql("UPDATE pb_accounts SET supporter=1 WHERE id=?", (account_id,))
            self._sql("INSERT INTO pb_receipts(token_hash,account_id,product_id,created,amount,remaining,pending_token) VALUES(?,?,?,?,?,?,?)", (token_hash, account_id, product_id, int(self.now()), amount, amount, encrypted_token))
            self._ledger(account_id, amount, "purchase:" + product_id)
            return {"duplicate": False, "coins": amount, "product_id": product_id}

    def pending_purchases(self):
        with self._lock:
            return [dict(row) for row in self._sql("SELECT token_hash,account_id,product_id,pending_token FROM pb_receipts WHERE pending_token<>'' AND voided=0 ORDER BY created LIMIT 100").fetchall()]

    def finalize_purchase(self, token_hash):
        with self.transaction():
            self._sql("UPDATE pb_receipts SET pending_token='' WHERE token_hash=?", (token_hash,))

    def commerce_checkpoint(self, value=None):
        with self.transaction():
            row = self._sql("SELECT value FROM pb_meta WHERE key='commerce_reconciled_ms'").fetchone()
            if value is not None:
                if row:
                    self._sql("UPDATE pb_meta SET value=? WHERE key='commerce_reconciled_ms'", (str(value),))
                else:
                    self._sql("INSERT INTO pb_meta(key,value) VALUES('commerce_reconciled_ms',?)", (str(value),))
            return int(row["value"]) if row else 0

    def revoke_purchase_verified(self, token_hash):
        """Called only for Google's authenticated Voided Purchases feed."""
        with self.transaction():
            receipt = self._sql("SELECT * FROM pb_receipts WHERE token_hash=?", (token_hash,)).fetchone()
            if not receipt:
                return {"known": False}
            account = self._sql("SELECT * FROM pb_accounts WHERE id=?" + (" FOR UPDATE" if self.postgres else ""), (receipt["account_id"],)).fetchone()
            receipt = self._sql("SELECT * FROM pb_receipts WHERE token_hash=?", (token_hash,)).fetchone()
            if receipt["voided"]:
                return {"known": True, "duplicate": True}
            amount = 0 if account["deleted"] else min(receipt["remaining"], account["coins"])
            spent = receipt["amount"] - receipt["remaining"]
            self._sql("UPDATE pb_receipts SET voided=1,remaining=0,pending_token='' WHERE token_hash=?", (token_hash,))
            if not account["deleted"]:
                self._sql("UPDATE pb_accounts SET coins=coins-?,store_review=CASE WHEN ?>0 THEN 1 ELSE store_review END WHERE id=?", (amount, int(spent > 0), account["id"]))
                if receipt["product_id"] == "supporter_coffee":
                    others = self._sql("SELECT COUNT(*) AS n FROM pb_receipts WHERE account_id=? AND product_id='supporter_coffee' AND voided=0", (account["id"],)).fetchone()["n"]
                    self._sql("UPDATE pb_accounts SET supporter=? WHERE id=?", (int(others > 0), account["id"]))
                self._ledger(account["id"], -amount, "verified_refund:" + receipt["product_id"])
            return {"known": True, "duplicate": False, "coins_removed": amount, "store_review": spent > 0, "account_id": account["id"]}

    def delete_account(self, account_id, confirmation, op):
        operation_id(op)
        if confirmation != "DELETE":
            raise EconomyError("confirmation_required", "Confirm account deletion explicitly")
        with self.transaction():
            self._account(account_id, lock=True)
            for table in ("pb_owned", "pb_operations", "pb_ledger", "pb_ad_challenges"):
                self._sql("DELETE FROM " + table + " WHERE account_id=?", (account_id,))
            # A minimal tombstone retains non-transferable receipt references and prevents
            # a consumed token from being redeemed again after deletion. No name/email exists.
            self._sql("UPDATE pb_accounts SET deleted=1,created=0,coins=0,rating=0,ranked_games=0,wins=0,supporter=0,reward_at=0,equipped='{}' WHERE id=?", (account_id,))
        return {"deleted": True, "retained": "pseudonymous purchase and match fraud-prevention records"}

    def create_match(self, match_id, players, mode, seed):
        if len(set(players)) != 2 or mode not in ("ranked", "casual"):
            raise EconomyError("invalid_match", "A match needs two distinct player accounts")
        with self.transaction():
            for player in sorted(players):
                self._account(player, lock=True)
            self._sql("INSERT INTO pb_matches(id,player0,player1,mode,seed,created,state,receipt) VALUES(?,?,?,?,?,?,?,?)", (match_id, *players, mode, seed, int(self.now()), "pending", "{}"))

    def match(self, match_id):
        with self._lock:
            row = self._sql("SELECT * FROM pb_matches WHERE id=?", (match_id,)).fetchone()
            if not row:
                raise EconomyError("unknown_match", "This match is not eligible for rewards")
            return dict(row)

    def repeated_opponents(self, players):
        row = self._sql("SELECT COUNT(*) AS count FROM pb_matches WHERE state='verified' AND created>? AND ((player0=? AND player1=?) OR (player0=? AND player1=?))", (int(self.now())-3600, *players, *reversed(players))).fetchone()
        return row["count"] >= 3

    def settle_verified(self, match_id, *, winner, frames, replay_hash):
        """Internal verifier boundary. Network dispatch NEVER accepts these values."""
        if type(winner) is not int or winner not in (0, 1) or not 1800 <= frames <= 54000:
            raise EconomyError("invalid_result", "The verifier result is outside the rewarded match rules")
        with self.transaction():
            match = self.match(match_id)
            # Stable account locking also serializes concurrent settlements involving a player.
            accounts = {player: self._account(player, lock=True) for player in sorted([match["player0"], match["player1"]])}
            match = self.match(match_id)
            if match["state"] != "pending":
                return json.loads(match["receipt"])
            if self.now() - match["created"] + 5 < frames / 60:
                raise EconomyError("invalid_duration", "The replay ran faster than real time")
            players = [match["player0"], match["player1"]]
            repeated = self.repeated_opponents(players)
            awards = [0, 0] if repeated else ([40, 40] if match["mode"] == "ranked" else [20, 20])
            if not repeated:
                awards[winner] += 20 if match["mode"] == "ranked" else 10
            deltas = [0, 0]
            if match["mode"] == "ranked" and not repeated:
                expected = 1 / (1 + 10 ** (max(-1600, min(1600, accounts[players[1]]["rating"] - accounts[players[0]]["rating"])) / 400))
                k = 32 if min(accounts[p]["ranked_games"] for p in players) < 10 else 24
                change = round(k * ((1 if winner == 0 else 0) - expected))
                change = max(-accounts[players[0]]["rating"], min(accounts[players[1]]["rating"], change))
                deltas = [change, -change]
            for index, player in enumerate(players):
                self._sql("UPDATE pb_accounts SET coins=coins+?,rating=rating+?,ranked_games=ranked_games+?,wins=wins+? WHERE id=?", (awards[index], deltas[index], int(match["mode"] == "ranked" and not repeated), int(match["mode"] == "ranked" and not repeated and index == winner), player))
                self._ledger(player, awards[index], "match:" + match_id)
            receipt = {"match_id": match_id, "status": "verified", "winner": winner, "awards": awards, "rating_deltas": deltas,
                       "frames": frames, "replay_hash": replay_hash, "reason": "repeat_opponent_practice" if repeated else "completed"}
            self._sql("UPDATE pb_matches SET state='verified',receipt=? WHERE id=?", (encode(receipt), match_id))
            return receipt

    def reject_match(self, match_id, reason):
        with self.transaction():
            match = self.match(match_id)
            for player in sorted([match["player0"], match["player1"]]):
                self._account(player, lock=True, include_deleted=True)
            match = self.match(match_id)
            if match["state"] != "pending":
                return json.loads(match["receipt"])
            receipt = {"match_id": match_id, "status": "rejected", "awards": [0, 0], "rating_deltas": [0, 0], "reason": reason}
            self._sql("UPDATE pb_matches SET state='rejected',receipt=? WHERE id=?", (encode(receipt), match_id))
            return receipt

    def expired_pending_matches(self):
        with self._lock:
            return [dict(row) for row in self._sql("SELECT id,player0,player1 FROM pb_matches WHERE state='pending' AND created<? ORDER BY created LIMIT 128", (int(self.now())-1200,)).fetchall()]
