# Pong-Breaker! lobby service — operator guide

This bundle contains only the Python rendezvous service and deployment files.
It lists public rooms, pairs Quick Match players, admits private invitation
codes, coordinates readiness, and forwards bounded WebRTC SDP/ICE messages.
The two game clients first attempt WebRTC gameplay. If that cannot connect,
the paired lobby can switch to a bounded gameplay relay through the same WSS
connection. The host player's device still simulates the game. No separate TURN
account is required for this built-in fallback; operator-configured UDP TURN
remains an optional WebRTC route.

Deploy this server independently from the game client. A public source repository
should contain only these server files, without credentials, development caches,
or unrelated files. The free Render configuration below uses no payment method
or paid resource.

## Local startup

Install Python 3.11+ and run from the extracted bundle's root:

```powershell
python -m pip install -r requirements.txt
python rendezvous.py
```

The default listener is `127.0.0.1:8765`, reachable by the game on this same
machine at `ws://127.0.0.1:8765/signal`. Stop with Ctrl+C. `/healthz` returns
constant readiness information without identities, rooms, or credentials.
Rooms and queues are in memory and disappear on restart. The pinned dependency
is the MIT-licensed [websockets](https://github.com/python-websockets/websockets)
package; no dependency binaries are bundled here.

The full game source includes `server/run_tests.py` and its 33-test native
loopback suite. Test outputs, caches, and development fixtures are deliberately
excluded from this deployment bundle. `PROTOCOL.md` describes the wire format.

## Free Render deployment from a public repository

Put the seven files from this bundle at the root of a public **server-only
repository**. In a free Render workspace with **no payment method**, choose
**New → Web Service → Public Git Repository**, paste that repository's HTTPS
URL, and connect it. This public-source route does not require connecting a
GitHub account or installing the Render GitHub App.

Configure the service as follows. Explicitly select Python even though an
optional Dockerfile is also present:

| Setting | Value |
| --- | --- |
| Language / runtime | Python 3 |
| Branch | The branch containing the uploaded server files |
| Root directory | Leave blank; files are at the repository root |
| Build command | `pip install -r requirements.txt` |
| Start command | `python rendezvous.py --host 0.0.0.0 --max-per-ip 128` |
| Compute plan | **Free** |
| Health check path | `/healthz` |
| Environment | `ALLOW_GAMEPLAY_RELAY=1` |
| Environment | `STUN_URL=stun:stun.l.google.com:19302` |
| Automatic deploys | **Off**; public-URL services do not support auto-deploys |

Use the provider-supplied `PORT` environment variable; the server reads it
automatically. No database, disk, paid workspace, or TURN secret is required.
Review that only one free web service is being created, then deploy. Source
updates on the public-URL route require a manual deploy from Render.
[Render web-service setup](https://render.com/docs/web-services).

The included `render.yaml` describes the equivalent single-service Blueprint
configuration: free Python runtime, automatic deploys off, and `/healthz`.
It is an optional alternative to the manual form, not a file that the manual
public-repository flow automatically applies.

Once Render reports the service live, verify the assigned HTTPS `/healthz`
response and WSS `/signal` connection. Enter
`wss://<assigned-host>.onrender.com/signal` on both game clients. Test an
invitation, public browser, Quick Match, and gameplay fallback across two
different networks. Test a separate UDP TURN allocation only if configured.

Render documents 750 free instance-hours per workspace/month, possible sleep
after 15 idle minutes, and about a minute to wake. Free accounts without payment
details are suspended when relevant allowances are exhausted; adding a payment
method can permit charges. A free hobby service has no uptime promise. These
limits were checked on 2026-10-05 and must be reviewed at deployment. Relayed
gameplay consumes host bandwidth; the free allowance is not unlimited traffic.
[Free service limits](https://render.com/docs/free),
[Billing FAQ](https://render.com/docs/faq).

The client waits up to 90 seconds for initial socket establishment and displays
a waking-service status after 20 seconds. Post-open operations remain bounded;
it does not silently replay room creation or joins.

## Other hosts or optional container

Place the service behind trusted HTTPS/WSS with WebSocket upgrade support. Proxy
`/signal` and `/healthz`, allow native clients with no Origin header, disable
buffering for WebSocket traffic, and use an idle timeout above 90 seconds.
The game accepts cleartext `ws://` only on loopback. The optional Dockerfile runs
as an unprivileged numeric user. A development container's published port should
bind loopback; validate the container on the target platform before deploying.

`--host` intentionally controls binding, and `PORT` controls the port. The
service does not trust forwarded client-IP headers. Default per-IP capacity is
16; behind a shared reverse proxy use appropriate edge limits and, if needed,
`--max-per-ip 128`, as the Blueprint does. The overall cap remains 128 sockets
and 64 rooms. No arbitrary browser origins are allowed by default; an actual
web client requires an explicit `--allowed-origin https://your-game.example`.

## STUN and optional TURN

Default STUN is `stun:stun.l.google.com:19302`; set `STUN_URL` to a comma-separated
replacement list, or an empty value for isolated local testing. STUN discovers
routes and does not relay gameplay. Direct connectivity is preferred but cannot
be guaranteed across every network. The included WSS gameplay fallback provides
the alternative when direct connectivity fails; TURN is optional.

An operator-owned coturn-compatible service can supply temporary TURN REST
credentials using these **server environment variables**, never APK constants:

```text
TURN_URL=turn:your-turn.example:3478?transport=udp
TURN_SHARED_SECRET=<server-only secret matching the TURN server>
TURN_TTL_SECONDS=3600
```

Configure URL and secret together. The broker gives each client an expiring
username/HMAC credential without disclosing the shared secret. Set allocation,
bandwidth, destination, and expiry limits on the TURN host; anonymous game
clients can request these temporary credentials. The bundle does not provision
a TURN server.

The game's pinned `webrtc-native 1.1.1-stable` uses libjuice and supports
**UDP TURN only**, with at most two relay servers. Do not configure `turns:`
or `transport=tcp` as a fallback for these binaries: the pinned implementation
ignores those transports. If the network blocks required UDP traffic, a working
WSS lobby does not provide a WebRTC path. The built-in WSS gameplay fallback can
be used instead. Real UDP TURN allocation is still a deployment check.
[Native build backend](https://github.com/godotengine/webrtc-native/blob/1.1.1-stable/tools/rtc.py),
[Pinned relay implementation](https://github.com/paullouisageneau/libdatachannel/blob/443f6934d9007eb7076ab7825ba330f355fcbead/src/impl/icetransport.cpp).

Managed TURN providers have their own usage limits and billing rules. To keep
deployment free, use only a verified no-charge allowance with no payment method,
purchased balance, or automatic top-up. A provider-specific credential API
adapter may be required and is not included. The built-in WSS fallback avoids
this separate provider dependency.

## Service limits and recovery

Gameplay fallback is enabled by default; `ALLOW_GAMEPLAY_RELAY=0` disables it.
Only a paired lobby may switch, and the first switch clears both Ready states.
The mode survives rematches but cannot change during an active match. Game
packets are opaque 1..8192-byte values encoded as canonical base64, with modes
and channels limited to integers 0..2. A separate bucket allows 180 packets per
second and a 256-packet burst. Membership/session checks select the sole other
player; no caller-supplied identity or target is accepted. Outgoing queues hold
at most 64 messages; slow recipients and repeated rate violations disconnect.

Relayed packets all travel reliably in order, which can increase latency under
loss. WSS encrypts client-to-server transport, but the service operator can see
forwarded bytes; it is not end-to-end encryption between players. The client
must label this mode as relay and keep normal snapshot/RPC validation enabled.

Rooms have two players, six-character random codes, and 16-character names.
Messages are at most 16 KiB. Server-owned identities and session tokens choose
the only authorized peer for signaling. Message/action rates and output queues
are bounded. Quick Match expires after three minutes, waiting lobbies after
15 minutes, and application silence after 90 seconds. A disconnect dissolves
the room; explicit rejoining creates a fresh identity. There is no account
authentication, resumable session, host migration, or automatic requeue.

For WebRTC, the game displays peer connection state without claiming to have
verified whether ICE selected direct or TURN traffic; the WSS fallback is
identified separately. Internet routes and a real TURN allocation remain
deployment tests, separate from passing PC loopback runs.
# Release 1.4 progression deployment note

The release now includes durable player accounts, an independent Godot replay verifier, and optional verified commerce. Follow [PROGRESSION.md](PROGRESSION.md) for the current Docker build, complete server package, PostgreSQL configuration, environment gates, refund policy and no-cost staging sequence. The native-Python commands and single-file service description below document the earlier guest matchmaking deployment. They remain useful for understanding the existing live service, but they are not sufficient to enable the new economy. No production database or payment/ad credential is included, and this local release work did not deploy them.

