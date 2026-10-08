# Pong-Breaker! release service — operator guide

The release service provides matchmaking, authenticated collections, ranked and casual rewards, and an independent Godot replay verifier. Gameplay first uses WebRTC between the players. A bounded WSS relay is available when direct connectivity fails. The host sequences inputs and the stock guest independently simulates them; server-side replay verification validates completed eligible matches before any reward or rating change. See [PROGRESSION.md](PROGRESSION.md) for the account, wallet, replay, refund and backup contract.

## Current deployment

On October 7, 2026, the owner approved and created the **pong-breaker-live** Render Free Docker service and a separate **Neon Free PostgreSQL 18** database named `pongbreaker`, both in Virginia. No payment method or paid resource was added. Source is the existing [server-only repository](https://github.com/JackMaxwell123/pong-breaker-lobby-service). This package targets **1.5.0-rc.1 / PONG_BREAKER_1_5**. Verify the exact deployed Git commit in Render after manual deployment; a Git push alone is not a deployment. The game workspace records the final commit and artifact hashes separately.

- Matchmaking: `wss://pong-breaker-live.onrender.com/signal`
- Readiness: `https://pong-breaker-live.onrender.com/healthz`
- Render service: `srv-db389gvlot8c73ev5jdg`
- Automatic deployment: **Off**
- Saved accounts, ranked matches and verified rewards: enabled
- Paid purchases and rewarded advertisements: disabled until their separate release requirements are met

The previous guest-only service remains unchanged, with automatic deployment off, so existing builds are not silently migrated. The client must explicitly use the new endpoint to receive progression. Existing and new services share the free workspace's allowances; retire the old service only after an intentional migration of its users.

The 1.5 server package contains **23 explicit allowlisted files**, including public privacy and deletion resources. Client and verifier simulation bytes must match. Local native paired tests cover direct play, forced relay and temporary lobby disconnects. The current server suite passes with the pinned dependencies, real local Godot replay and signed offline commerce fixtures; two optional fresh PostgreSQL integration fixtures need an explicitly supplied staging URL. An earlier real Neon transaction/restore drill passed. Neither that historical drill nor desktop testing certifies Android behavior or future backup operation.

After deployment, verify `/healthz`, an actual WSS handshake, `/privacy`, `/account`, persisted fixture progress and a completed paired native match. Delete only the test runner's own temporary accounts. Private reports and deployment receipts are kept in the game workspace, outside this public package.

## Reproduce the deployment

Publish only the explicit 23-file server allowlist. Preserve the `verifier/` directory and the exact client `simulation.gd` bytes. Never publish the game workspace, environment files, backups, database URLs, recovery keys or store credentials.

| Setting | Value |
| --- | --- |
| Service name | `pong-breaker-live` |
| Runtime | Docker |
| Branch | `main` |
| Root directory | Empty: the server files are at the repository root |
| Dockerfile / context | `./Dockerfile` / `.` |
| Compute plan | **Free**, with no payment method |
| Region | Virginia, near the PostgreSQL project |
| Health check | `/healthz` |
| Automatic deployment | **Off** |
| `PB_DATABASE_URL` | Secret TLS PostgreSQL URL, configured only in provider environment settings |
| `PB_DURABLE_STORAGE` | `1` only after durable storage and a successful restore drill |
| `PB_GODOT_BINARY` | `/opt/pong-verifier/godot` |
| `PB_REPLAY_SIMULATION` | `/app/verifier/simulation.gd` |
| `ALLOW_GAMEPLAY_RELAY` | `1` |
| `STUN_URL` | `stun:stun.l.google.com:19302` |

The Docker build installs pinned Python dependencies and downloads the official Linux x86-64 Godot 4.7.2 archive, verifying its pinned SHA-512 checksum before extraction. The service runs as an unprivileged numeric user and uses Render's `PORT`. The Blueprint in `render.yaml` describes the equivalent configuration; a manually created service does not automatically apply later Blueprint edits.

Use a persistent external PostgreSQL database. Render's free web filesystem is ephemeral and its free PostgreSQL database expires; never enable a production wallet on that filesystem. The approved deployment uses Neon Free. Keep the database URL secret and require TLS. Test backup/restore with `backup_restore.py` as documented in [PROGRESSION.md](PROGRESSION.md); do not treat database connectivity alone as a backup policy.

Check **GET** `/healthz` and an actual WSS handshake after every runtime deployment. A plain browser request to `/signal`, a missing WebSocket upgrade header, or a platform `HEAD /` probe is not a valid gameplay handshake and may be rejected. Health checks should use the configured GET endpoint.

## Local development and legacy compatibility

For guest-only matchmaking, install `requirements.txt` and run `python rendezvous.py`. It listens on `127.0.0.1:8765`; the local client URL is `ws://127.0.0.1:8765/signal`. Without durable storage and a configured verifier, ranked play and rewards fail closed while legacy casual/private rooms remain available.

For full progression development, also install `requirements-progression.txt`, configure an isolated local SQLite database or test PostgreSQL database, and point `PB_GODOT_BINARY` at the verified matching Godot 4.7.2 executable. SQLite is for local development unless an operator explicitly attests a genuinely persistent volume. It is not the Render Free deployment database. The full game workspace includes the tests; development fixtures are deliberately excluded from this server-only bundle.

## Costs, quotas and recovery

The current provider plans have no recurring charge while they remain Free without a payment method. Quotas still apply. Render free services can sleep after inactivity and take time to wake, and their instance-hour and bandwidth allowances are shared across the workspace. Exhausted free allowances can make the service unavailable. Neon has storage and compute allowances; idle guest-only activity does not poll the database continually. Review actual free usage before adding users or enabling commerce reconciliation. Never enable paid upgrades, automatic top-ups or paid instances without the owner's separate approval. See [Render Free limits](https://render.com/docs/free) and [Neon Free plan details](https://neon.com/blog/neon-free-plan-1-gb-per-project).

Keep encrypted backups and their encryption key separately protected outside the repository. Run periodic restore drills and verify a known recovery key, wallet, owned cosmetic and idempotent purchase receipt. No automatic backup schedule is created by this package. A service restart preserves committed PostgreSQL progress but loses active rooms and incomplete in-memory replay uploads. Returning participants receive a no-contest restart receipt; stale abandoned records expire without invented wins or currency awards.

Only a paired room may switch to the relay, and it cannot change transport during live play. Relay packets are bounded to 8,192 bytes, with session checks, rate limits and bounded output queues. The relay encrypts transport to and from the service; it is not end-to-end encryption between players. See [PROTOCOL.md](PROTOCOL.md) for limits and [PROGRESSION.md](PROGRESSION.md) for anti-abuse and release limits.

## Optional TURN

The built-in WSS relay needs no separate TURN account. An operator-owned coturn-compatible server can instead be configured through `TURN_URL`, `TURN_SHARED_SECRET` and `TURN_TTL_SECONDS`; the shared secret stays server-side. The pinned WebRTC backend supports UDP TURN, not TCP/TLS TURN. A configured TURN allocation requires its own real-network test and free-quota review. No TURN service is provisioned by this package.

## Candidate rollout and rollback

Deploy the server-only 1.5 package before sharing the matching APK. Automatic deploy stays off. The protocol boundary prevents old/new physics from sharing an eligible match; prior builds may require an update for ranked play. Avoid deployments during active matches: a process replacement cannot preserve in-memory replay buffers. Committed wallets stay in PostgreSQL.

Confirm Render still shows Free, no payment method is required, advertisements and purchases remain false, and the advertised simulation hash matches the delivered game. If verification fails, use Render's existing rollback control to redeploy the previous known-good commit and withhold the new client. There is no database schema migration in this pass; credential rotation uses the existing key column.

Public resources: `/privacy` and `/account`; support **PipeDreamInt@gmail.com**. Restore old backups only while access is closed and after reconciling later account deletions. A named operator, recurring protected backups and quota/capacity review remain requirements before a paid public launch.
