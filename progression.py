"""WebSocket progression extension and authenticated replay settlement boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import urlsplit

from economy import CATALOG, EconomyError, REWARD_COINS, operation_id
from replay_verifier import ReplayUpload, MAX_FRAMES, CHUNK_FRAMES

COMMANDS = {"account_auth", "account_delete", "profile_get", "catalog_get", "shop_buy", "cosmetic_equip", "reward_prepare", "reward_claim", "reward_status", "purchase_submit", "replay_chunk", "replay_seal", "match_receipt_get"}


class Progression:
    def __init__(self, broker, economy=None, verifier=None, stores=None, receipt_cipher=None):
        self.broker, self.economy, self.verifier, self.stores = broker, economy, verifier, stores
        self.uploads = {}
        self.tasks = set()
        self.client_locks = {}
        self.busy = {}
        self.pending_jobs = 0
        self._store_slots = asyncio.Semaphore(4)
        self._ad_slots = asyncio.Semaphore(4)
        self.receipt_cipher = receipt_cipher
        self._reconciled = False
        self._commerce_check_at = 0.0
        self._commerce_check_running = False
        self._pending_expiry_at = 0.0

    def capabilities(self):
        durable = bool(self.economy and self.economy.durable)
        verified = bool(durable and self.verifier)
        stores = self.stores.capabilities() if self.stores else {}
        billing = bool(durable and stores.get("billing", False) and self.receipt_cipher and self._reconciled)
        return {"economy": durable, "ranked": verified, "match_rewards": verified,
                "billing": billing, "rewarded_ads": durable and stores.get("rewarded_ads", False),
                "ads": durable and stores.get("rewarded_ads", False), "purchases": billing,
                "progression_version": 1}

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    def require_account(self, client):
        if not self.capabilities()["economy"]:
            raise EconomyError("economy_unavailable", "Player accounts and rewards are waiting for durable server storage")
        if not client.account_id:
            raise EconomyError("account_required", "Connect your saved player account first")

    def _emit_profile(self, client, *, request_id=None, event_type="profile", **extra):
        profile = self.economy.profile(client.account_id)
        client.profile = profile
        self.broker._emit(client, {"type": event_type, "profile": profile, "capabilities": self.capabilities(), "request_id": request_id, **extra})

    def dispatch(self, client, kind, msg):
        if kind not in COMMANDS:
            return False
        if self.busy.get(client.id, 0) >= 8:
            self.broker._error(client, "progression_busy", "Wait for the previous account operation to finish", msg["request_id"])
            return True
        self.busy[client.id] = self.busy.get(client.id, 0) + 1
        self._task(self._serialized(client, kind, msg))
        return True

    async def _serialized(self, client, kind, msg):
        lock = self.client_locks.setdefault(client.id, asyncio.Lock())
        async with lock:
            try:
                await self._dispatch(client, kind, msg)
            except EconomyError as error:
                self.broker._error(client, error.code, error.message, msg["request_id"])
            except Exception as error:
                if hasattr(error, "code") and hasattr(error, "message"):
                    self.broker._error(client, error.code, error.message, msg["request_id"])
                    return
                # Never log receipts, bearer tokens, connection strings or incoming requests.
                logging.error("Progression action failed (%s)", type(error).__name__)
                self.broker._error(client, "progression_unavailable", "The account service could not complete this request; your balance has not been replaced", msg["request_id"])
            finally:
                self.busy[client.id] -= 1
                if self.busy[client.id] == 0:
                    self.busy.pop(client.id, None)
                    if client.closing or client.id not in self.broker.clients:
                        self.client_locks.pop(client.id, None)

    async def _db(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _profile(self, client, msg, event_type="profile", **extra):
        profile = await self._db(self.economy.profile, client.account_id)
        client.profile = profile
        capabilities = self.capabilities()
        if profile.get("store_review"):
            capabilities.update(billing=False, purchases=False)
        self.broker._emit(client, {"type": event_type, "request_id": msg.get("request_id"), "profile": profile, "capabilities": capabilities, **extra})

    async def _dispatch(self, client, kind, msg):
        fields = self.broker._fields
        if not self.capabilities()["economy"]:
            raise EconomyError("economy_unavailable", "Player accounts and rewards are waiting for durable server storage")
        if kind == "account_auth":
            fields(msg, {"token"})
            self.broker._require_idle(client)
            if client.account_id and not msg.get("token"):
                raise EconomyError("already_authenticated", "This connection already has a player account")
            account_id, token = await self._db(self.economy.authenticate, msg.get("token"))
            client.account_id = account_id
            await self._profile(client, msg, "account", **({"token": token} if token else {}), catalog=CATALOG)
            return
        self.require_account(client)
        if kind == "account_delete":
            fields(msg, {"confirmation", "operation_id"})
            self.broker._require_idle(client)
            account_id = client.account_id
            result = await self._db(self.economy.delete_account, account_id, msg.get("confirmation"), msg.get("operation_id"))
            for other in list(self.broker.clients.values()):
                if other.account_id == account_id:
                    self.broker._remove_from_queue(other)
                    self.broker._dissolve(other.room, "account_deleted")
                    other.account_id, other.profile = None, {}
                    self.broker._emit(other, {"type": "account_deleted", **result, "request_id": msg["request_id"] if other is client else None})
        elif kind in ("profile_get", "reward_status"):
            fields(msg, set())
            await self._profile(client, msg)
        elif kind == "catalog_get":
            fields(msg, set())
            self.broker._emit(client, {"type": "catalog", "catalog": CATALOG, "request_id": msg["request_id"]})
        elif kind == "shop_buy":
            fields(msg, {"item_id", "operation_id"})
            result = await self._db(self.economy.buy, client.account_id, msg.get("item_id"), msg.get("operation_id"))
            await self._profile(client, msg, "purchase", result=result)
        elif kind == "cosmetic_equip":
            fields(msg, {"slot", "item_id", "operation_id"})
            if client.room and self.broker.rooms.get(client.room) and self.broker.rooms[client.room].phase == "playing":
                raise EconomyError("match_active", "Finish this match before changing your shared cosmetics")
            result = await self._db(self.economy.equip, client.account_id, msg.get("slot"), msg.get("item_id"), msg.get("operation_id"))
            await self._profile(client, msg, "equipped", result=result)
            room = self.broker.rooms.get(client.room or "")
            if room:
                self.broker._broadcast_room(room)
        elif kind == "reward_prepare":
            fields(msg, {"operation_id"})
            if not self.capabilities()["ads"]:
                raise EconomyError("ads_unavailable", "Optional rewarded ads have not been configured for this release")
            result = await self._db(self.economy.prepare_ad, client.account_id, msg.get("operation_id"))
            self.broker._emit(client, {"type": "reward_prepared", **result, "request_id": msg["request_id"]})
        elif kind == "reward_claim":
            fields(msg, {"operation_id"})
            result = await self._db(self.economy.claim_supporter, client.account_id, msg.get("operation_id"))
            await self._profile(client, msg, "reward_granted", result=result)
        elif kind == "purchase_submit":
            fields(msg, {"product_id", "purchase_token", "operation_id"})
            operation_id(msg.get("operation_id"))
            if not self.capabilities()["purchases"]:
                raise EconomyError("purchases_unavailable", "Google Play purchases have not been configured for this release")
            token, product = msg.get("purchase_token"), msg.get("product_id")
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or not isinstance(product, str):
                raise EconomyError("bad_purchase", "This Google Play purchase has an invalid token or product")
            profile = await self._db(self.economy.profile, client.account_id)
            if profile.get("store_review"):
                raise EconomyError("store_review", "Paid purchases are paused while a refunded purchase is reviewed; gameplay and earned cosmetics remain available")
            async with self._store_slots:
                verified = await asyncio.to_thread(self.stores.verify_purchase, product, token, profile["billing_account_id"])
                result = await self._db(self.economy.apply_purchase_verified, client.account_id, encrypted_token=self.receipt_cipher.encrypt(token), **verified)
                try:
                    await asyncio.to_thread(self.stores.finalize_purchase, product, token)
                    await self._db(self.economy.finalize_purchase, verified["token_hash"])
                    finalized = True
                except Exception:
                    # A retry uses Google's token uniqueness and safely finalizes the same grant.
                    finalized = False
            await self._profile(client, msg, "purchase", result={**result, "finalized": finalized})
        elif kind == "replay_chunk":
            fields(msg, {"match_id", "offset", "frames"})
            upload = self._upload(msg.get("match_id"), client.account_id)
            offset = upload.append(client.account_id, msg.get("offset"), msg.get("frames"))
            self.broker._emit(client, {"type": "replay_ack", "match_id": msg["match_id"], "offset": offset, "request_id": msg["request_id"]})
        elif kind == "replay_seal":
            fields(msg, {"match_id", "sha256", "frames"})
            upload = self._upload(msg.get("match_id"), client.account_id)
            if upload.verifying:
                self.broker._emit(client, {"type": "replay_pending", "match_id": msg["match_id"], "request_id": msg["request_id"]})
                return
            try:
                ready = upload.seal(client.account_id, msg.get("sha256"), msg.get("frames"))
            except EconomyError as error:
                if error.code == "replay_disputed":
                    receipt = await self._db(self.economy.reject_match, msg["match_id"], "replay_disputed")
                    await self._notify_receipt(upload.players, receipt)
                    self.uploads.pop(msg["match_id"], None)
                raise
            self.broker._emit(client, {"type": "replay_pending", "match_id": msg["match_id"], "request_id": msg["request_id"]})
            if ready:
                self._task(self._verify(msg["match_id"], upload))
        elif kind == "match_receipt_get":
            fields(msg, {"match_id"})
            match = await self._db(self.economy.match, msg.get("match_id"))
            players = (match["player0"], match["player1"])
            if client.account_id not in players:
                raise EconomyError("wrong_match", "This account is not a player in this match")
            if match["state"] == "pending":
                self.broker._emit(client, {"type": "replay_pending", "match_id": match["id"], "request_id": msg["request_id"]})
            else:
                await self._notify_receipt(players, json.loads(match["receipt"]), client=client, request_id=msg["request_id"])

    def _upload(self, match_id, account_id):
        if not isinstance(match_id, str) or match_id not in self.uploads:
            raise EconomyError("unknown_match", "This match is not awaiting a replay")
        upload = self.uploads[match_id]
        if account_id not in upload.players:
            raise EconomyError("wrong_match", "This account is not a player in this match")
        return upload

    def round_start(self, room, seed):
        self._task(self._register_start(room, seed))

    async def _register_start(self, room, seed):
        match_id = room.round_id
        eligible = bool(room.matchmade and self.capabilities()["match_rewards"] and len(self.uploads) + self.pending_jobs < 8)
        clients = [self.broker.clients.get(identity) for identity in room.players]
        players = tuple(client.account_id if client else None for client in clients)
        eligible = eligible and len(set(players)) == 2 and all(players)
        eligible = eligible and all(client and client.game_protocol == "PONG_BREAKER_1_4" for client in clients)
        if room.mode == "ranked" and not eligible:
            self.broker._dissolve(room.code, "ranked_temporarily_unavailable")
            return
        if eligible:
            self.pending_jobs += 1
            try:
                await self._db(self.economy.create_match, match_id, players, room.mode, seed)
                self.uploads[match_id] = ReplayUpload(players, seed, time.monotonic())
            except Exception:
                if room.mode == "ranked":
                    self.broker._dissolve(room.code, "progression_unavailable")
                    return
                eligible = False  # Guest/casual play survives an unavailable wallet DB.
            finally:
                self.pending_jobs -= 1
        if self.broker.rooms.get(room.code) is not room or room.round_id != match_id:
            return
        self.broker._broadcast(room, {"type": "start", "session_id": room.session_id, "round_id": match_id,
                                     "match_id": match_id, "seed": seed, "mode": room.mode, "reward_eligible": bool(eligible),
                                     "replay": {"version": 1, "fps": 60, "max_frames": MAX_FRAMES, "chunk_frames": CHUNK_FRAMES,
                                                "simulation_sha256": self.verifier.simulation_hash if self.verifier else ""}})

    async def _verify(self, match_id, upload):
        try:
            result = await self.verifier.verify(upload.seed, upload.frames[0])
            receipt = await self._db(self.economy.settle_verified, match_id, winner=result["winner"], frames=result["frames"], replay_hash=upload.seals[0])
        except EconomyError as error:
            receipt = await self._db(self.economy.reject_match, match_id, error.code)
        except Exception:
            receipt = await self._db(self.economy.reject_match, match_id, "verification_unavailable")
        finally:
            self.uploads.pop(match_id, None)
        await self._notify_receipt(upload.players, receipt)

    async def _notify_receipt(self, players, receipt, client=None, request_id=None):
        targets = [client] if client else list(self.broker.clients.values())
        for target in targets:
            if target.account_id not in players:
                continue
            index = players.index(target.account_id)
            event = {key: value for key, value in receipt.items() if key not in ("awards", "rating_deltas")}
            event.update(coins=receipt["awards"][index], rating_delta=receipt["rating_deltas"][index])
            await self._profile(target, {"request_id": request_id}, "match_receipt", **event)

    async def sweep(self, now):
        for match_id, upload in tuple(self.uploads.items()):
            if not upload.verifying and now - upload.created > 1200:
                receipt = await self._db(self.economy.reject_match, match_id, "replay_expired")
                self.uploads.pop(match_id, None)
                await self._notify_receipt(upload.players, receipt)
        if self.economy and (self.broker.clients or self.uploads) and now >= self._pending_expiry_at:
            self._pending_expiry_at = now + 60
            try:
                for match in await self._db(self.economy.expired_pending_matches):
                    if match["id"] in self.uploads:
                        continue
                    receipt = await self._db(self.economy.reject_match, match["id"], "replay_expired")
                    await self._notify_receipt((match["player0"], match["player1"]), receipt)
            except Exception:
                logging.warning("Pending replay expiry will retry after the database is available")
        if self.economy and self.economy.durable and self.stores and self.receipt_cipher and self.stores.capabilities().get("billing") and not self._commerce_check_running and now >= self._commerce_check_at:
            self._commerce_check_running = True
            self._commerce_check_at = now + 1800
            self._task(self._reconcile_commerce())

    async def _reconcile_commerce(self):
        try:
            async with self._store_slots:
                cursor = ""
                end = int(time.time() * 1000)
                previous = await self._db(self.economy.commerce_checkpoint)
                if previous and previous < end - 29 * 86400000:
                    raise EconomyError("reconciliation_gap", "The Google refund history gap requires operator review")
                # Overlap is intentional; token-hash revocation is idempotent.
                for _ in range(100):
                    page = await asyncio.to_thread(self.stores.list_voided_purchases, end - 29 * 86400000, end, cursor)
                    for item in page.get("purchases", []):
                        result = await self._db(self.economy.revoke_purchase_verified, item["token_hash"])
                        if result.get("known") and not result.get("duplicate"):
                            for client in list(self.broker.clients.values()):
                                if client.account_id == result.get("account_id"):
                                    await self._profile(client, {}, "profile", notice="Google Play purchase status was updated")
                    cursor = page.get("next_page_token", "")
                    if not cursor:
                        break
                else:
                    raise EconomyError("reconciliation_capacity", "Purchase reconciliation requires operator review")
                for pending in await self._db(self.economy.pending_purchases):
                    try:
                        token = self.receipt_cipher.decrypt(pending["pending_token"])
                        await asyncio.to_thread(self.stores.finalize_purchase, pending["product_id"], token)
                        await self._db(self.economy.finalize_purchase, pending["token_hash"])
                    except Exception:
                        logging.warning("A purchase finalization is awaiting a safe retry")
                await self._db(self.economy.commerce_checkpoint, end)
                self._reconciled = True
        except Exception:
            self._reconciled = False
            self._commerce_check_at = time.monotonic() + 60
            logging.warning("Paid purchases remain disabled until authenticated reconciliation succeeds")
        finally:
            self._commerce_check_running = False

    async def ad_callback(self, raw_query):
        if not self.capabilities()["ads"]:
            raise EconomyError("ads_unavailable", "Rewarded ad verification is not enabled")
        if len(raw_query) > 8192:
            raise EconomyError("invalid_receipt", "Reward callback exceeds its size limit")
        async with self._ad_slots:
            verified = await asyncio.to_thread(self.stores.verify_ad_callback, raw_query)
            return await self._db(self.economy.apply_ad_verified, **verified)
