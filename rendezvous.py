"""Bounded two-player rendezvous with optional WebSocket gameplay fallback."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
import base64
import binascii
import hashlib
import hmac
from http import HTTPStatus
import json
import logging
import os
import re
import secrets
import time
import unicodedata

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from economy import Economy, EconomyError
from progression import Progression
from replay_verifier import GodotReplayVerifier


PROTOCOL = "PONG_BREAKER_WEBRTC_1"
GAME_PROTOCOL = "PONG_BREAKER_1_4"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")
MAX_MESSAGE = 16384
MAX_RELAY_PACKET = 8192


class RequestError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


@dataclass(frozen=True)
class Limits:
    clients: int = 128
    per_ip: int = 16
    rooms: int = 64
    outbox: int = 64
    hello_seconds: float = 10.0
    idle_seconds: float = 90.0
    queue_seconds: float = 180.0
    lobby_seconds: float = 900.0
    sweep_seconds: float = 1.0
    rate_per_second: float = 20.0
    rate_burst: float = 80.0
    action_rate: float = 2.0
    action_burst: float = 12.0
    relay_rate: float = 180.0
    relay_burst: float = 256.0


@dataclass(frozen=True)
class IceConfig:
    stun_urls: tuple[str, ...] = ("stun:stun.l.google.com:19302",)
    turn_urls: tuple[str, ...] = ()
    turn_secret: str = field(default="", repr=False)
    credential_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "IceConfig":
        def urls(key: str, default: str) -> tuple[str, ...]:
            raw = os.environ.get(key, default)
            values = tuple(x.strip() for x in raw.split(",") if x.strip())
            if len(values) > 8 or any(len(x) > 256 or any(c.isspace() for c in x) for x in values):
                raise ValueError(f"Invalid {key} configuration")
            return values

        stun = urls("STUN_URL", "stun:stun.l.google.com:19302")
        turn = urls("TURN_URL", "")
        if any(not x.startswith(("stun:", "stuns:")) for x in stun):
            raise ValueError("STUN_URL must contain stun: or stuns: URLs")
        if any(not x.startswith(("turn:", "turns:")) for x in turn):
            raise ValueError("TURN_URL must contain turn: or turns: URLs")
        secret = os.environ.get("TURN_SHARED_SECRET", "")
        if bool(turn) != bool(secret):
            raise ValueError("TURN_URL and TURN_SHARED_SECRET must be configured together")
        ttl = int(os.environ.get("TURN_TTL_SECONDS", "3600"))
        if not 300 <= ttl <= 86400:
            raise ValueError("TURN_TTL_SECONDS must be between 300 and 86400")
        return cls(stun, turn, secret, ttl)

    def for_client(self, client_id: str) -> dict:
        servers = [{"urls": list(self.stun_urls)}] if self.stun_urls else []
        expires = None
        if self.turn_urls and self.turn_secret:
            expires = int(time.time()) + self.credential_seconds
            username = f"{expires}:{client_id}"
            credential = base64.b64encode(hmac.new(self.turn_secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
            servers.append({"urls": list(self.turn_urls), "username": username, "credential": credential})
        return {"ice_servers": servers, "ice_expires_at": expires}


@dataclass
class Bucket:
    tokens: float
    rate: float
    capacity: float
    updated: float

    def take(self, now: float) -> bool:
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


@dataclass
class Client:
    id: str
    socket: ServerConnection
    address: str
    outbox: asyncio.Queue[str]
    bucket: Bucket
    actions: Bucket
    relay_bucket: Bucket
    connected_at: float
    name: str = ""
    hello: bool = False
    room: str | None = None
    ready: bool = False
    queued_at: float | None = None
    closing: bool = False
    violations: int = 0
    seen_ids: set[str] = field(default_factory=set)
    recent_ids: deque[str] = field(default_factory=deque)
    account_id: str | None = None
    profile: dict = field(default_factory=dict)
    queue_mode: str = "casual"
    game_protocol: str = ""


@dataclass
class Room:
    code: str
    public: bool
    players: list[str]
    session_id: str
    created: float
    phase: str = "lobby"
    round_id: str | None = None
    offer_sent: bool = False
    answer_sent: bool = False
    relay_enabled: bool = False
    mode: str = "casual"
    matchmade: bool = False


class Rendezvous:
    def __init__(self, *, limits: Limits | None = None, ice: IceConfig | None = None,
                 allow_gameplay_relay: bool = True, economy=None, verifier=None, stores=None, receipt_cipher=None):
        self.limits = limits or Limits()
        self.ice = ice or IceConfig()
        self.allow_gameplay_relay = allow_gameplay_relay
        self.clients: dict[str, Client] = {}
        self.rooms: dict[str, Room] = {}
        self.waiting: deque[str] = deque()
        self._lock = asyncio.Lock()
        self._server: Server | None = None
        self._sweep: asyncio.Task | None = None
        self._closers: set[asyncio.Task] = set()
        self._closing = False
        self.progression = Progression(self, economy, verifier, stores, receipt_cipher)

    async def start(self, host: str = "127.0.0.1", port: int = 8765,
                    allowed_origins: tuple[str, ...] = ()) -> Server:
        if self._server is not None or self._closing:
            raise RuntimeError("Service already started or closed")

        async def route(connection, request):
            if request.path == "/healthz":
                return connection.respond(HTTPStatus.OK, "Pong-Breaker rendezvous ready\n")
            if request.path.startswith("/rewards/admob?"):
                try:
                    await self.progression.ad_callback(request.path.split("?", 1)[1])
                    return connection.respond(HTTPStatus.OK, "Verified\n")
                except Exception:
                    return connection.respond(HTTPStatus.BAD_REQUEST, "Unverified reward\n")
            if request.path not in ("/", "/signal"):
                return connection.respond(HTTPStatus.NOT_FOUND, "Unknown endpoint\n")
            return None

        self._server = await serve(
            self.handle, host, port, origins=[None, *allowed_origins],
            process_request=route, compression=None, max_size=MAX_MESSAGE,
            max_queue=8, write_limit=32768, open_timeout=5,
            ping_interval=20, ping_timeout=20, close_timeout=2,
            server_header=None,
        )
        self._sweep = asyncio.create_task(self._maintenance())
        return self._server

    async def close(self) -> None:
        self._closing = True
        if self._sweep is not None:
            self._sweep.cancel()
            await asyncio.gather(self._sweep, return_exceptions=True)
            self._sweep = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._closers:
            await asyncio.gather(*tuple(self._closers), return_exceptions=True)
        await self.progression.close()

    def _close_later(self, client: Client, code: int, reason: str) -> None:
        if client.closing:
            return
        client.closing = True
        task = asyncio.create_task(client.socket.close(code, reason))
        self._closers.add(task)
        task.add_done_callback(self._closers.discard)

    def _emit(self, client: Client, event: dict) -> None:
        if client.closing:
            return
        message = json.dumps({"v": 1, **event}, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        try:
            client.outbox.put_nowait(message)
        except asyncio.QueueFull:
            self._close_later(client, 1013, "Signaling consumer too slow")

    def _error(self, client: Client, code: str, message: str, request_id=None) -> None:
        self._emit(client, {"type": "error", "code": code, "message": message, "request_id": request_id})

    async def _writer(self, client: Client) -> None:
        try:
            while not client.closing:
                message = await client.outbox.get()
                if client.closing:
                    return
                await asyncio.wait_for(client.socket.send(message), timeout=5)
        except (ConnectionClosed, asyncio.TimeoutError):
            self._close_later(client, 1013, "Signaling output unavailable")

    async def handle(self, socket: ServerConnection) -> None:
        address = str(socket.remote_address[0]) if socket.remote_address else "unknown"
        now = time.monotonic()
        async with self._lock:
            counts = Counter(c.address for c in self.clients.values())
            if self._closing or len(self.clients) >= self.limits.clients or counts[address] >= self.limits.per_ip:
                client = None
            else:
                identity = secrets.token_hex(16)
                client = Client(identity, socket, address, asyncio.Queue(self.limits.outbox),
                                Bucket(self.limits.rate_burst, self.limits.rate_per_second, self.limits.rate_burst, now),
                                Bucket(self.limits.action_burst, self.limits.action_rate, self.limits.action_burst, now),
                                Bucket(self.limits.relay_burst, self.limits.relay_rate, self.limits.relay_burst, now), now)
                self.clients[identity] = client
        if client is None:
            await socket.send(json.dumps({"v": 1, "type": "error", "code": "capacity", "message": "Signaling server is full"}))
            await socket.close(1013, "Connection capacity")
            return

        writer = asyncio.create_task(self._writer(client))
        try:
            while not client.closing:
                timeout = self.limits.idle_seconds if client.hello else max(0.0, client.connected_at + self.limits.hello_seconds - time.monotonic())
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout)
                except asyncio.TimeoutError:
                    await socket.close(1008, "Hello or heartbeat timeout")
                    break
                async with self._lock:
                    self._dispatch_raw(client, raw)
                # Give each connection's output pump fair service during incoming bursts.
                await asyncio.sleep(0)
        except ConnectionClosed:
            pass
        finally:
            async with self._lock:
                self._remove_from_queue(client)
                self._dissolve(client.room, "peer_disconnected" if not self._closing else "server_shutdown")
                self.clients.pop(client.id, None)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    def _dispatch_raw(self, client: Client, raw: str | bytes) -> None:
        request_id = None
        try:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE:
                raise RequestError("bad_message", "Use a bounded UTF-8 JSON text message")
            try:
                message = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite number")))
            except (ValueError, RecursionError):
                raise RequestError("bad_message", "Malformed JSON") from None
            if not isinstance(message, dict):
                raise RequestError("bad_message", "JSON message must be an object")
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
                request_id = None
                raise RequestError("bad_message", "request_id must be a short unique identifier")
            if type(message.get("v", 1)) is not int or message.get("v", 1) != 1:
                raise RequestError("bad_protocol", "Unsupported protocol version")
            now = time.monotonic()
            kind = message.get("type")
            if not isinstance(kind, str):
                raise RequestError("bad_message", "Message type is required")
            bucket = client.relay_bucket if kind == "relay_packet" else client.bucket
            if not bucket.take(now):
                raise RequestError("rate_limited", "Too many relay packets" if kind == "relay_packet" else "Too many signaling messages")
            if request_id in client.seen_ids:
                raise RequestError("duplicate_request", "Request ID already used on this connection")
            client.seen_ids.add(request_id)
            client.recent_ids.append(request_id)
            if len(client.recent_ids) > 512:
                client.seen_ids.discard(client.recent_ids.popleft())
            if kind in {"list_rooms", "create_room", "join_room", "quick_match", "ice_config"} and not client.actions.take(now):
                raise RequestError("rate_limited", "Too many lobby operations")
            self._dispatch(client, kind, message, now)
        except (RequestError, EconomyError) as error:
            self._error(client, error.code, error.message, request_id)
            if error.code in {"bad_message", "bad_protocol", "hello_required", "already_hello", "duplicate_request", "rate_limited"}:
                client.violations += 1
                if client.violations >= 3:
                    self._close_later(client, 1008, "Repeated invalid requests")

    @staticmethod
    def _fields(message: dict, allowed: set[str]) -> None:
        if set(message) - {"v", "type", "request_id"} - allowed:
            raise RequestError("bad_message", "Unknown message fields")

    @staticmethod
    def _text(value, max_chars: int, *, label: str, empty: bool = False) -> str:
        if not isinstance(value, str) or (not value and not empty) or len(value) > max_chars or "\x00" in value:
            raise RequestError("bad_message", f"Invalid {label}")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise RequestError("bad_message", f"Invalid {label}")
        return value

    def _require_room(self, client: Client) -> Room:
        room = self.rooms.get(client.room or "")
        if room is None or client.id not in room.players:
            raise RequestError("not_in_room", "Join a room first")
        return room

    def _require_idle(self, client: Client) -> None:
        if client.room is not None or client.queued_at is not None:
            raise RequestError("busy", "Leave your room or cancel matchmaking first")

    def _ack(self, client: Client, message: dict) -> None:
        self._emit(client, {"type": "ack", "action": message["type"], "request_id": message["request_id"]})

    def _dispatch(self, client: Client, kind: str, msg: dict, now: float) -> None:
        if kind == "hello":
            self._fields(msg, {"name", "protocol"})
            if client.hello:
                raise RequestError("already_hello", "Identity is fixed until disconnect")
            if msg.get("protocol") != PROTOCOL:
                raise RequestError("bad_protocol", "Use a compatible Pong-Breaker client")
            name = self._text(msg.get("name"), 16, label="player name").strip()
            if not name or any(unicodedata.category(char).startswith("C") for char in name):
                raise RequestError("bad_message", "Invalid player name")
            client.name, client.hello = name, True
            self._emit(client, {"type": "welcome", "protocol": PROTOCOL, "client_id": client.id,
                               "heartbeat_seconds": 20, **self.ice.for_client(client.id),
                               "gameplay_relay": self.allow_gameplay_relay,
                               "progression_version": 1, "capabilities": self.progression.capabilities(),
                               "limits": {"message_bytes": MAX_MESSAGE, "name_characters": 16,
                                          "queue_seconds": self.limits.queue_seconds,
                                          "relay_packet_bytes": MAX_RELAY_PACKET,
                                          "relay_packets_per_second": self.limits.relay_rate},
                               "request_id": msg["request_id"]})
            return
        if not client.hello:
            raise RequestError("hello_required", "Send hello before lobby commands")
        if self.progression.dispatch(client, kind, msg):
            return
        if kind == "ping":
            self._fields(msg, set())
            self._emit(client, {"type": "pong", "request_id": msg["request_id"]})
        elif kind == "ice_config":
            self._fields(msg, set())
            self._emit(client, {"type": "ice_config", **self.ice.for_client(client.id), "request_id": msg["request_id"]})
        elif kind == "list_rooms":
            self._fields(msg, set())
            rooms = [{"code": room.code, "name": self.clients[room.players[0]].name,
                      "players": 1, "public": True}
                     for room in self.rooms.values()
                     if room.public and room.phase == "lobby" and len(room.players) == 1
                     and not self.clients[room.players[0]].closing]
            self._emit(client, {"type": "rooms", "rooms": rooms, "request_id": msg["request_id"]})
        elif kind == "create_room":
            self._fields(msg, {"public"})
            self._require_idle(client)
            if type(msg.get("public", False)) is not bool:
                raise RequestError("bad_message", "public must be a boolean")
            room = self._create([client], msg.get("public", False), now)
            self._ack(client, msg)
            self._broadcast_room(room)
        elif kind == "join_room":
            self._fields(msg, {"code"})
            self._require_idle(client)
            code = self._text(msg.get("code"), 6, label="invitation code").upper()
            room = self.rooms.get(code)
            if room is None or any(self.clients[identity].closing for identity in room.players):
                raise RequestError("room_unavailable", "Room not found or no longer available")
            if room.phase != "lobby" or len(room.players) >= 2:
                raise RequestError("room_full", "Room already has two players")
            room.players.append(client.id)
            client.room = code
            client.ready = False
            self._ack(client, msg)
            self._broadcast_room(room)
        elif kind == "quick_match":
            self._fields(msg, {"mode", "game_protocol"})
            self._require_idle(client)
            mode = msg.get("mode", "casual")
            game_protocol = msg.get("game_protocol", "")
            if not isinstance(game_protocol, str) or (game_protocol and re.fullmatch(r"[A-Z0-9_]{1,48}", game_protocol) is None):
                raise RequestError("bad_message", "Invalid gameplay protocol")
            if mode not in ("ranked", "casual"):
                raise RequestError("bad_message", "Match mode must be ranked or casual")
            if mode == "ranked":
                self.progression.require_account(client)
                if not self.progression.capabilities()["ranked"]:
                    raise RequestError("ranked_unavailable", "Ranked play is waiting for durable storage and independent replay verification")
                if game_protocol != GAME_PROTOCOL:
                    raise RequestError("game_update_required", "Update Pong-Breaker before entering ranked play")
            client.queue_mode = mode
            client.game_protocol = game_protocol
            candidate = next((self.clients.get(identity) for identity in self.waiting
                              if identity in self.clients and not self.clients[identity].closing
                              and self.clients[identity].queued_at is not None
                              and now - self.clients[identity].queued_at < self.limits.queue_seconds
                              and self._compatible(client, self.clients[identity], now)), None)
            if candidate is not None:
                room = self._create([candidate, client], False, now, mode=mode, matchmade=True)
                self._remove_from_queue(candidate)
                self._ack(client, msg)
                self._emit(candidate, {"type": "queue", "queued": False, "reason": "matched"})
                self._emit(client, {"type": "queue", "queued": False, "reason": "matched"})
                self._broadcast_room(room)
            else:
                client.queued_at = now
                self.waiting.append(client.id)
                self._ack(client, msg)
                self._emit(client, {"type": "queue", "queued": True, "mode": mode})
        elif kind == "cancel_queue":
            self._fields(msg, set())
            self._remove_from_queue(client)
            self._ack(client, msg)
            self._emit(client, {"type": "queue", "queued": False, "reason": "cancelled"})
        elif kind == "leave_room":
            self._fields(msg, set())
            self._ack(client, msg)
            self._dissolve(client.room, "peer_left")
        elif kind == "ready":
            self._fields(msg, {"value"})
            room = self._require_room(client)
            if room.phase != "lobby":
                raise RequestError("wrong_phase", "Match already started")
            if len(room.players) != 2:
                raise RequestError("peer_unavailable", "Wait for an opponent")
            if type(msg.get("value")) is not bool:
                raise RequestError("bad_message", "Ready value must be boolean")
            client.ready = msg["value"]
            self._ack(client, msg)
            if all(self.clients[identity].ready for identity in room.players):
                room.phase = "playing"
                room.round_id = secrets.token_hex(12)
                seed = secrets.randbelow(2147483646) + 1
                for identity in room.players:
                    self.clients[identity].ready = False
                self._broadcast_room(room)
                self.progression.round_start(room, seed)
            else:
                self._broadcast_room(room)
        elif kind == "round_complete":
            self._fields(msg, {"round_id"})
            room = self._require_room(client)
            if room.players[0] != client.id:
                raise RequestError("wrong_role", "Only host can complete a round")
            if room.phase != "playing":
                raise RequestError("wrong_phase", "No active round")
            if msg.get("round_id") != room.round_id:
                raise RequestError("stale_round", "Round token no longer matches")
            room.phase, room.round_id, room.created = "lobby", None, now
            for identity in room.players:
                self.clients[identity].ready = False
            self._ack(client, msg)
            self._broadcast_room(room)
        elif kind == "signal":
            self._signal(client, msg)
        elif kind == "relay_request":
            self._relay_request(client, msg)
        elif kind == "relay_packet":
            self._relay_packet(client, msg)
        else:
            raise RequestError("bad_message", "Unknown message type")

    def _relay_room(self, client: Client, msg: dict) -> tuple[Room, Client, int]:
        if not self.allow_gameplay_relay:
            raise RequestError("relay_disabled", "Gameplay fallback is disabled by this service")
        room = self._require_room(client)
        if msg.get("session_id") != room.session_id:
            raise RequestError("stale_session", "Relay belongs to another room session")
        if len(room.players) != 2:
            raise RequestError("peer_unavailable", "Wait for an opponent")
        peer_id = room.players.index(client.id) + 1
        opponent = self.clients[room.players[1 if peer_id == 1 else 0]]
        if opponent.closing:
            raise RequestError("peer_unavailable", "Opponent is disconnecting")
        return room, opponent, peer_id

    def _relay_request(self, client: Client, msg: dict) -> None:
        self._fields(msg, {"session_id"})
        room, _, _ = self._relay_room(client, msg)
        if room.phase != "lobby":
            raise RequestError("wrong_phase", "Cannot change transport during an active match")
        self._ack(client, msg)
        if room.relay_enabled:
            # A second simultaneous request must not reinstall the transport
            # or clear readiness acquired after the first switch.
            return
        room.relay_enabled = True
        for identity in room.players:
            self.clients[identity].ready = False
        self._broadcast(room, {"type": "relay_ready", "session_id": room.session_id,
                               "reason": "peer_requested_fallback"})
        self._broadcast_room(room)

    def _relay_packet(self, client: Client, msg: dict) -> None:
        self._fields(msg, {"session_id", "channel", "mode", "payload"})
        room, opponent, peer_id = self._relay_room(client, msg)
        if not room.relay_enabled:
            raise RequestError("relay_not_enabled", "Negotiate gameplay fallback before sending packets")
        channel, mode = msg.get("channel"), msg.get("mode")
        if type(channel) is not int or not 0 <= channel <= 2 or type(mode) is not int or not 0 <= mode <= 2:
            raise RequestError("bad_message", "Relay channel and mode must be integers from 0 to 2")
        payload = msg.get("payload")
        if not isinstance(payload, str) or not 1 <= len(payload) <= 4 * ((MAX_RELAY_PACKET + 2) // 3):
            raise RequestError("bad_message", "Invalid relay payload size")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            raise RequestError("bad_message", "Relay payload must be canonical base64") from None
        if not 1 <= len(decoded) <= MAX_RELAY_PACKET or base64.b64encode(decoded).decode("ascii") != payload:
            raise RequestError("bad_message", "Relay payload must be bounded canonical base64")
        # Bytes stay opaque: never decode native Variants or execute game logic.
        # No sender acknowledgement: one bounded outgoing event per packet.
        self._emit(opponent, {"type": "relay_packet", "session_id": room.session_id,
                              "from_id": client.id, "from_peer_id": peer_id,
                              "channel": channel, "mode": mode, "payload": payload})

    def _signal(self, client: Client, msg: dict) -> None:
        kind = msg.get("kind")
        allowed = {"session_id", "kind"}
        allowed |= {"sdp"} if kind in ("offer", "answer") else {"candidate", "mid", "mline_index"}
        self._fields(msg, allowed)
        room = self._require_room(client)
        if msg.get("session_id") != room.session_id:
            raise RequestError("stale_session", "Signaling belongs to another room session")
        if len(room.players) != 2:
            raise RequestError("peer_unavailable", "Wait for an opponent")
        peer_id = room.players.index(client.id) + 1
        opponent = self.clients[room.players[1 if peer_id == 1 else 0]]
        if opponent.closing:
            raise RequestError("peer_unavailable", "Opponent is disconnecting")
        event = {"type": "signal", "session_id": room.session_id, "kind": kind,
                 "from_id": client.id, "from_peer_id": peer_id}
        if kind in ("offer", "answer"):
            sdp = self._text(msg.get("sdp"), 12288, label="SDP")
            if not sdp.startswith("v=0") or len(sdp.encode("utf-8")) > 12288:
                raise RequestError("bad_message", "Invalid SDP")
            if peer_id != (1 if kind == "offer" else 2):
                raise RequestError("wrong_role", "Host offers; guest answers")
            if (kind == "offer" and room.offer_sent) or (kind == "answer" and (not room.offer_sent or room.answer_sent)):
                raise RequestError("invalid_signal_state", "Unexpected SDP negotiation step")
            event["sdp"] = sdp
        elif kind == "ice":
            candidate = self._text(msg.get("candidate"), 2048, label="ICE candidate", empty=True)
            mid = self._text(msg.get("mid"), 64, label="ICE media ID", empty=True)
            index = msg.get("mline_index")
            if type(index) is not int or not 0 <= index <= 64 or "\n" in candidate or "\r" in candidate:
                raise RequestError("bad_message", "Invalid ICE candidate")
            if candidate and not candidate.startswith("candidate:"):
                raise RequestError("bad_message", "Invalid ICE candidate")
            event.update(candidate=candidate, mid=mid, mline_index=index)
        else:
            raise RequestError("bad_message", "Unknown signaling kind")
        # Forwarding adds server-owned identity fields. A near-limit incoming
        # frame must not become an oversized frame at the receiving client.
        encoded = json.dumps({"v": 1, **event}, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            raise RequestError("bad_message", "Forwarded signaling exceeds the message limit")
        if kind == "offer":
            room.offer_sent = True
        elif kind == "answer":
            room.answer_sent = True
        self._ack(client, msg)
        self._emit(opponent, event)

    def _compatible(self, first: Client, second: Client, now: float) -> bool:
        if first.queue_mode != second.queue_mode or first.game_protocol != second.game_protocol or (first.account_id and first.account_id == second.account_id):
            return False
        if first.queue_mode != "ranked":
            return True
        first_rating = first.profile.get("ranked", {}).get("rating", 1000)
        second_rating = second.profile.get("ranked", {}).get("rating", 1000)
        waited = max(now - (first.queued_at or now), now - (second.queued_at or now))
        return abs(first_rating - second_rating) <= min(600, 150 + int(waited / 15) * 75)

    def _create(self, players: list[Client], public: bool, now: float, *, mode="casual", matchmade=False) -> Room:
        if len(self.rooms) >= self.limits.rooms:
            raise RequestError("capacity", "Room capacity reached; try again later")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        while code in self.rooms:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        room = Room(code, public, [client.id for client in players], secrets.token_hex(16), now)
        room.mode, room.matchmade = mode, matchmade
        self.rooms[code] = room
        for client in players:
            client.room, client.ready = code, False
        return room

    def _snapshot(self, room: Room) -> dict:
        return {"code": room.code, "public": room.public, "session_id": room.session_id,
                "phase": room.phase, "round_id": room.round_id, "host_id": room.players[0],
                "relay_enabled": room.relay_enabled,
                "mode": room.mode, "matchmade": room.matchmade,
                "players": [{"client_id": identity, "peer_id": number + 1,
                             "name": self.clients[identity].name, "ready": self.clients[identity].ready,
                             "cosmetics": self.clients[identity].profile.get("equipped", {}),
                             "equipped": self.clients[identity].profile.get("equipped", {}),
                             "ranked": self.clients[identity].profile.get("ranked", {})}
                            for number, identity in enumerate(room.players)]}

    def _broadcast(self, room: Room, event: dict) -> None:
        for identity in room.players:
            self._emit(self.clients[identity], event)

    def _broadcast_room(self, room: Room) -> None:
        self._broadcast(room, {"type": "room", "room": self._snapshot(room)})

    def _remove_from_queue(self, client: Client) -> None:
        client.queued_at = None
        try:
            self.waiting.remove(client.id)
        except ValueError:
            pass

    def _dissolve(self, code: str | None, reason: str) -> None:
        room = self.rooms.pop(code or "", None)
        if room is None:
            return
        for identity in room.players:
            client = self.clients.get(identity)
            if client is not None:
                client.room, client.ready = None, False
                self._emit(client, {"type": "room_closed", "code": room.code,
                                    "session_id": room.session_id, "reason": reason})

    async def sweep_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        async with self._lock:
            for identity in tuple(self.waiting):
                client = self.clients.get(identity)
                if client is not None and client.queued_at is not None and now - client.queued_at >= self.limits.queue_seconds:
                    self._remove_from_queue(client)
                    self._emit(client, {"type": "queue", "queued": False, "reason": "expired"})
            for room in tuple(self.rooms.values()):
                if room.phase == "lobby" and now - room.created >= self.limits.lobby_seconds:
                    self._dissolve(room.code, "lobby_expired")
            # A widening skill window can match two already waiting players.
            queued = [self.clients[identity] for identity in self.waiting if identity in self.clients and not self.clients[identity].closing]
            for index, first in enumerate(queued):
                if first.queued_at is None:
                    continue
                second = next((other for other in queued[index+1:] if other.queued_at is not None and self._compatible(first, other, now)), None)
                if second and len(self.rooms) < self.limits.rooms:
                    room = self._create([first, second], False, now, mode=first.queue_mode, matchmade=True)
                    for client in (first, second):
                        self._remove_from_queue(client)
                        self._emit(client, {"type": "queue", "queued": False, "reason": "matched"})
                    self._broadcast_room(room)
        await self.progression.sweep(now)

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(self.limits.sweep_seconds)
            await self.sweep_once()


async def run(args) -> None:
    config = IceConfig.from_env()
    relay_setting = os.environ.get("ALLOW_GAMEPLAY_RELAY", "1").lower()
    if relay_setting not in {"0", "1", "false", "true"}:
        raise ValueError("ALLOW_GAMEPLAY_RELAY must be 0/1 or false/true")
    economy = Economy.from_environment()
    verifier = GodotReplayVerifier.from_environment()
    try:
        from store_verification import StoreVerifier, ReceiptCipher
        stores = StoreVerifier.from_environment()
        receipt_cipher = ReceiptCipher.from_environment()
    except ImportError:
        stores = None
        receipt_cipher = None
    broker = Rendezvous(limits=Limits(per_ip=args.max_per_ip), ice=config,
                        allow_gameplay_relay=relay_setting in {"1", "true"}, economy=economy, verifier=verifier, stores=stores, receipt_cipher=receipt_cipher)
    await broker.start(args.host, args.port, tuple(args.allowed_origin))
    print(json.dumps({"event": "listening", "host": args.host, "port": args.port,
                      "protocol": PROTOCOL, "gameplay_relay": broker.allow_gameplay_relay,
                      "turn_configured": bool(config.turn_urls)}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await broker.close()
        if economy:
            economy.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--max-per-ip", type=int, default=16)
    parser.add_argument("--allowed-origin", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.max_per_ip <= 128:
        parser.error("Port must be 1..65535 and max-per-ip 1..128")
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
