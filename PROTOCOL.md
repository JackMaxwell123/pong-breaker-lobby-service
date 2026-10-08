# Pong-Breaker rendezvous protocol 1

This native WebSocket service introduces peers and coordinates their lobby.
WebRTC is the first gameplay transport. If it cannot connect, the paired lobby
can switch to bounded opaque gameplay packets over the same WebSocket service.
The host sequences gameplay inputs; release 1.5 stock guests independently
simulate that witnessed stream. The broker routes native game packets without
simulating live ball, paddle, or score state. Completed eligible matches are
independently replayed by the server-owned Godot verifier before rewards are
awarded; this does not change live peer transport. Optional TURN is an independent
ICE configuration, not a prerequisite for the WebSocket fallback.

Default endpoint: `ws://127.0.0.1:8765/signal`. `/` is also accepted. Native clients
must omit the HTTP Origin header. Browser clients require an explicit allowed
origin on the server. Production clients must use a trusted `wss://` endpoint.

Every client message is one UTF-8 JSON object with `type` and a unique
`request_id` matching `[A-Za-z0-9_-]{1,48}`. Optional `v` must equal integer `1`.
Unknown fields are rejected. IDs are remembered for the most recent 512 requests
per connection; duplicate requests are rejected, not replayed. Every direct
response echoes `request_id`. Events don't have one. Server messages contain `v:1`.

Send hello within 10 seconds:

```json
{"type":"hello","protocol":"PONG_BREAKER_WEBRTC_1","name":"Pilot","request_id":"1"}
```

Reply `welcome` includes `protocol`, immutable random `client_id`,
`heartbeat_seconds:20`, `ice_servers`, `ice_expires_at` (Unix seconds or null),
`gameplay_relay` boolean, and `limits`. Limits include `relay_packet_bytes:8192`
and `relay_packets_per_second:180`. `ice_servers` uses WebRTC-compatible objects:
`{"urls":["stun:host:3478"]}` or
`{"urls":["turn:host:3478"],"username":"expiry:identity","credential":"..."}`.
The shared TURN secret is never sent to clients. Credentials are short-lived.
`ice_config` refreshes this configuration for a later negotiation.

| Client type | Additional fields | Direct response |
|---|---|---|
| `list_rooms` | none | `rooms {rooms:[{code,name,players:1,public:true}]}` |
| `create_room` | `public` boolean, defaults false | `ack {action:"create_room"}` |
| `join_room` | `code` six-character invite, case-insensitive | `ack` |
| `quick_match` | none | `ack` |
| `cancel_queue` | none | `ack` |
| `leave_room` | none | `ack` |
| `ready` | `value` boolean | `ack` |
| `round_complete` | `round_id` returned in `start`; host only | `ack` |
| `signal` | see below | `ack` |
| `relay_request` | current `session_id` | `ack`, then first transition emits `relay_ready` and `room` to both peers |
| `relay_packet` | `session_id`, `channel`, `mode`, `payload` | no success response; error still echoes request ID |
| `ping` | none | `pong` |
| `ice_config` | none | `ice_config {ice_servers,ice_expires_at}` |

One connection can be in one room or one queue position. Creating, joining, or
queueing while in another activity is rejected: leave/cancel first. Cancelling
an absent queue or leaving an absent room is harmless. Public listings expose
only public rooms with one player waiting in a lobby. Private rooms are unlisted,
not authenticated: knowledge of their random invitation code grants admission.
Guest-only mode has no accounts or chat. The authenticated progression extension is described below.

Room snapshots are sent to both members after a membership/readiness change:

```json
{"v":1,"type":"room","room":{"code":"ABCD23","public":false,"session_id":"random-pair-epoch","phase":"lobby","round_id":null,"relay_enabled":false,"host_id":"immutable-client-id","players":[{"client_id":"immutable-client-id","peer_id":1,"name":"Pilot","ready":false},{"client_id":"second-id","peer_id":2,"name":"Rival","ready":false}]}}
```

The first player is host / Godot peer 1; the second is guest / peer 2. Host creates
one offer when the second member arrives. `session_id` stays fixed for that room
and is renewed with every new room. Guests answer; either peer can send ICE:

```json
{"type":"signal","request_id":"5","session_id":"random-pair-epoch","kind":"offer","sdp":"v=0\r\n..."}
{"type":"signal","request_id":"6","session_id":"random-pair-epoch","kind":"answer","sdp":"v=0\r\n..."}
{"type":"signal","request_id":"7","session_id":"random-pair-epoch","kind":"ice","candidate":"candidate:...","mid":"0","mline_index":0}
```

Forwarded `signal` events have the same signaling fields plus server-assigned
`from_id` and `from_peer_id`. Routing always uses the sender's current room and
its sole opponent. Client-supplied sender, target, peer, or room claims are invalid.
One offer and one answer are allowed per room, with host-before-guest ordering.
ICE may arrive before the offer; clients must buffer it until remote SDP exists.
Empty ICE candidate is allowed as an end-of-candidates marker.

The UI must enable READY only after the selected native peer transport is
established. The broker doesn't inspect that handshake. Both `ready:true` values atomically set
`phase:"playing"`, clear readiness, and emit a room snapshot and
`start {session_id,round_id,seed}` to both peers. Host starts the authoritative game
using the seed over the selected Godot RPC transport; the guest waits for that RPC. The
broker's `round_id` is a signaling lifecycle token, separate from any native game
simulation tick or seed epoch. Host `round_complete` with that token returns both
to `lobby` for a rematch. The selected transport remains active across rematches.

## WebSocket gameplay fallback

The server enables this capability by default. `ALLOW_GAMEPLAY_RELAY=0` disables
it, reports `gameplay_relay:false` in welcome, and rejects relay operations with
`relay_disabled`. It uses the already-open trusted WSS connection, so no separate
relay account or TURN allocation is required. A reachable hosted service is
still required for internet play.

After a failed/expired direct connection attempt, either peer may request:

```json
{"type":"relay_request","request_id":"8","session_id":"random-pair-epoch"}
```

Only a two-player room in `lobby` may switch. The first request is acknowledged,
sets `room.relay_enabled:true`, and clears both ready flags. Each member receives
`relay_ready {session_id,reason:"peer_requested_fallback"}` **before** the updated
room snapshot. Both clients install their fallback peer and perform their normal
game handshake before enabling Ready. Another request in the same lobby is
acknowledged without repeating the event, reinstalling the transport, or clearing
readiness again. A request during `playing` returns `wrong_phase`; there is no
mid-match downgrade or transparent recovery. The flag persists through rematches
but disappears with the room.

After negotiation, packets may flow in either lobby or playing phase:

```json
{"type":"relay_packet","request_id":"packet_1","session_id":"random-pair-epoch","channel":0,"mode":2,"payload":"AQID"}
```

`channel` and `mode` are exact integers 0..2 (booleans/floats rejected). `payload`
must be canonical standard base64 decoding to 1..8192 bytes. The decoded bytes
remain opaque; the service does not instantiate objects or decode Godot Variants.
Each packet needs a unique request ID, but receives **no success acknowledgement**
and must not be added to a pending-request/retry queue. Validation errors still
carry that request ID. Forwarding sends only the sole current room opponent:

```json
{"v":1,"type":"relay_packet","session_id":"random-pair-epoch","from_id":"broker-owned-sender-id","from_peer_id":1,"channel":0,"mode":2,"payload":"AQID"}
```

Sender/target fields supplied by a client are rejected. Session mismatch,
strangers, closing peers, or packets before relay negotiation cannot forward.
The packet bucket permits 180 per second with a 256-packet burst, independently
of signaling's 20/second bucket. Existing 16 KiB wire limits and 64-message
outboxes still apply; repeated violations or a slow recipient close the socket
and dissolve its room. This is bounded capacity, not unlimited free bandwidth.

WebSockets carry all modes reliably and in order, even when the metadata says
unreliable. Packet loss can delay subsequent packets (head-of-line blocking).
WSS protects each client-to-server connection; this fallback is not an end-to-end
encrypted peer channel, and the broker operator can access forwarded bytes.
The UI must identify it as relayed play, not direct P2P. Client-side snapshot
and RPC authorization checks remain necessary against a malicious opponent.

`quick_match` is FIFO. Waiting produces `queue {queued:true}`; cancellation,
expiry, or pairing produces `queue {queued:false,reason}`. Pairing creates a
private two-player room and emits its snapshots. It never silently joins an
existing public room. Queue wait expires after 180 seconds; press Quick Match
again if desired. No bots are represented as online players.

Any leave, dead socket, signaling failure cleanup, or server shutdown dissolves
the room for everyone: `room_closed {code,session_id,reason}`. No host migration,
resume, automatic requeue, or automatic native transport retry occurs. Remaining
clients may explicitly create/join/queue again. A reconnect gets a new identity.
Legacy guest clients close the native peer when the room closes. Release 1.5 eligible direct matches may keep their native peer while reconnecting and restoring their upload; they return to matchmaking afterward. Relay matches cannot continue without their service. Clients send `ping` every
20 seconds; application silence for 90 seconds and missing WebSocket pong
responses disconnect dead peers. Waiting lobbies expire after 15 minutes.

Errors: `error {code,message,request_id}`. Codes include `bad_message`,
`bad_protocol`, `hello_required`, `already_hello`, `duplicate_request`,
`busy`, `room_unavailable`, `room_full`, `not_in_room`, `peer_unavailable`,
`stale_session`, `wrong_role`, `invalid_signal_state`, `wrong_phase`,
`stale_round`, `relay_disabled`, `relay_not_enabled`, `capacity`, `rate_limited`. Three invalid messages or repeated rate
violations can close a connection with policy code 1008. Message size is capped
at 16 KiB; oversize frames close with code 1009. No error returns submitted SDP,
ICE contents, secrets, or other players' private room state.
# Release 1.5 extension

The original signaling protocol above remains compatible. Authenticated accounts, shops, mode-separated queues, replay verification and receipts are specified in [PROGRESSION.md](PROGRESSION.md). Feature-detect `welcome.progression_version` and capabilities before sending credentials or requests. A host's `round_complete` message never awards coins or rating.
