# Friends — backend

Self-hosted backend for **Friends**, a random 1-on-1 video chat app (React Native Android/iOS, web later).
Video and audio go **peer-to-peer over WebRTC**. This server only handles auth, signaling, matchmaking,
moderation, subscriptions and admin stats. It uses no third-party media, chat or BaaS services: just
FastAPI, PostgreSQL, Redis, coturn and Caddy on one VPS.

Repositories:
- Backend (this repo). The admin dashboard is a git submodule at `dashboard/`, so clone with
  `git clone --recurse-submodules https://github.com/thelllmike/friends_app_backend.git`
  (or run `git submodule update --init` after a plain clone; the Docker build needs it).
- Dashboard: https://github.com/thelllmike/friends_app_dashboard
- Mobile app: https://github.com/thelllmike/friends_app

```
 phone/web ──HTTPS/WSS──► Caddy :443 ──► FastAPI (uvicorn, N workers) ──► PostgreSQL
     │                                          │
     │                                          └──► Redis (queue, rooms, presence, rate limits, pub/sub)
     └──STUN/TURN (UDP/TCP 3478, TLS 5349)──► coturn  (relay ports 49152-65535/udp)
```

## Project layout

```
app/
  api/        REST routers: auth, me (profile), safety (reports/blocks), premium, turn, meta, admin
  core/       config (.env), db, redis, JWT/security, deps (auth + RBAC), errors, rate limiting, logging
  models/     SQLAlchemy 2 models (users, identities, tokens, blocks, reports, bans, rooms, plans, subs, admin)
  schemas/    Pydantic request/response models
  services/   business logic: identity (Google/Apple), otp, tokens, users, matchmaking, presence,
              moderation, blocks, billing + stores (Play/App Store), turn, stats, app_settings
  ws/         /ws endpoint, per-session protocol handler, cross-worker connection manager
  tasks/      background jobs (premium expiry, renewal checks, presence, online snapshots, stale rooms)
  cli.py      `python -m app.cli create-admin ...`
dashboard/    admin web dashboard (static HTML/CSS/JS, no build step), served at /dashboard/
alembic/      migration 0001 creates every table and seeds the 3 plans
scripts/e2e_two_users.py   two fake users: match → offer/answer/ICE → chat → next
scripts/seed_demo.py       local demo data for the dashboard (refuses to run in production)
tests/        integration tests (run against a live server)
Dockerfile, docker-compose.yml, Caddyfile, turnserver.conf, .env.example
```

---

## 1. Run locally (dev)

Requirements: Python 3.12, PostgreSQL 14+, Redis 6+.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# For dev, edit .env:  ENVIRONMENT=development  OTP_DEV_ECHO=true  BILLING_FAKE_MODE=true
#   DATABASE_URL=postgresql+asyncpg://friends:friends@localhost:5432/friends
#   REDIS_URL=redis://localhost:6379/0   JWT_SECRET=<anything>   TURN_SECRET=<anything>
#   MEDIA_DIR=./media  MEDIA_BASE_URL=http://127.0.0.1:8000/media  SEARCH_TIMEOUT_SECONDS=5

alembic upgrade head
python -m app.cli create-admin root@friends.test 'super-secret-pass-1' superadmin
uvicorn app.main:app --reload          # http://127.0.0.1:8000/docs
```

### Test it

```bash
# Two fake users: sign up, connect, match, exchange offer/answer/ICE, chat, then "next"
python scripts/e2e_two_users.py --api http://127.0.0.1:8000

# Full integration suite (39 tests). Needs the dev settings above plus these test admins:
python -m app.cli create-admin root@friends.test    'super-secret-pass-1' superadmin
python -m app.cli create-admin mod@friends.test     'moderator-pass-123'  moderator
python -m app.cli create-admin analyst@friends.test 'analyst-pass-1234'   analyst
pytest -q
```

### Admin dashboard

Open **http://127.0.0.1:8000/dashboard/** and sign in with an admin created by `app.cli create-admin`.
In production it is at `https://<API_DOMAIN>/dashboard/` (same origin as the API, so no CORS setup).

| Page | Needs role | What it does |
|---|---|---|
| Overview | analyst+ | online now (total/web/by platform), calls today, avg call length, open reports, each vs last week; calls per day (14/30/90 days); top countries; reports queue |
| Users | moderator+ | search and filter; profile drawer with reports, bans, subscriptions and recent calls; ban/unban; grant/revoke premium (admin+) |
| Calls | analyst+ | calls per day and average length per day; recent calls list (moderator+) |
| Reports | moderator+ | open / actioned / dismissed queue; review drawer with the call details: dismiss, resolve, or ban |
| Countries | analyst+ | users, weekly actives, premium and online now per country |
| Settings | analyst+ (read only) | feature flags and thresholds, plan prices (admin+), admin accounts (superadmin) |

The admin token is kept in `sessionStorage` (closing the tab signs out) and expires after
`ADMIN_TOKEN_MINUTES` (default 8 hours). Pages are served with a strict Content-Security-Policy.
To see it with data locally: `python -m scripts.seed_demo`.

The e2e script signs up over the login endpoints, which allow `LOGIN_RATE_PER_MINUTE` (default 10)
requests per IP per minute. If you run it many times in a row, wait a minute.

---

## 2. Deploy on a VPS

Tested layout: one Ubuntu 22.04/24.04 VPS (2 vCPU / 2–4 GB RAM is plenty to start) with a public IPv4.

### 2.1 DNS

Create two **A records** pointing to the VPS IP (turn off any CDN proxy such as Cloudflare's orange
cloud, because TURN must hit the server directly):

| Host               | Purpose                                   |
|--------------------|-------------------------------------------|
| `api.example.com`  | REST + WebSocket + profile photos (Caddy) |
| `turn.example.com` | STUN/TURN (coturn); Caddy gets its cert   |

### 2.2 Firewall

| Port            | Proto    | Service                                   |
|-----------------|----------|-------------------------------------------|
| 22              | TCP      | SSH                                       |
| 80              | TCP      | Caddy (ACME HTTP challenge, redirect)     |
| 443             | TCP+UDP  | Caddy HTTPS / WSS / HTTP3                 |
| 3478            | UDP+TCP  | STUN / TURN                               |
| 5349            | TCP+UDP  | TURN over TLS / DTLS                      |
| 49152–65535     | UDP      | TURN relay range                          |

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw allow 3478/udp && sudo ufw allow 3478/tcp
sudo ufw allow 5349/tcp && sudo ufw allow 5349/udp
sudo ufw allow 49152:65535/udp
sudo ufw enable
```
Also open the same ports in your provider's cloud firewall or security group, if it has one.

### 2.3 Install Docker & deploy

```bash
curl -fsSL https://get.docker.com | sh
git clone <your repo> friends && cd friends
cp .env.example .env
```
Fill in `.env`:
- `API_DOMAIN`, `TURN_DOMAIN`, `PUBLIC_IP` (the VPS's public IPv4), `ACME_EMAIL`
- `POSTGRES_PASSWORD`, `JWT_SECRET`, `TURN_SECRET`: generate each with `openssl rand -hex 32`
- `CORS_ORIGINS`: your web client and admin dashboard origins
- `GOOGLE_CLIENT_IDS`, `APPLE_AUDIENCES`
- Store billing keys (section 3). Put the key files in `./secrets/` (it's mounted read-only at `/app/secrets`).

```bash
docker compose up -d --build
docker compose logs -f api caddy            # wait for "certificate obtained" for both domains
docker compose restart coturn               # restart once both certs exist so coturn loads the TLS cert
docker compose exec api python -m app.cli create-admin you@example.com '<long password>' superadmin
curl https://api.example.com/health         # {"status":"ok","db":true,"redis":true}
```

The API refuses to start in production if `JWT_SECRET` or `TURN_SECRET` look like defaults, or if
`CORS_ORIGINS` is `*`. Migrations run automatically when the container starts. Each `.env` setting
has a comment in `.env.example`.

**TLS certificates:** Caddy issues and renews certificates for both domains automatically. coturn reads
the TURN certificate from Caddy's data volume, but only when it starts. Caddy renews about 30 days
before expiry, so restart coturn weekly to pick up renewals:
```bash
( crontab -l; echo '0 4 * * 1 cd /root/friends && docker compose restart coturn' ) | crontab -
```

**TURN over TLS on 443:** Caddy already uses 443 on the main IP. To also offer `turns:` on 443 (useful on
networks that block everything except 443), give the VPS a second IP. Then set `listening-ip=<second ip>`
and `alt-tls-listening-port=443` in `turnserver.conf`, and add `443` to `TURN_TLS_PORTS`.

**Scaling:** `WEB_CONCURRENCY` sets the number of uvicorn workers. Matchmaking is serialized with a
Redis lock, and WebSocket messages reach peers on other workers through Redis pub/sub. Background jobs
take Redis leader locks, so each job runs once per interval no matter how many workers there are.

### 2.4 Verify TURN

1. Get credentials: `curl -H "Authorization: Bearer <access>" https://api.example.com/turn-credentials`
2. Open https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/ and add
   `turn:turn.example.com:3478` with that username and credential. You should see `relay` candidates.
3. Or from any machine with coturn tools installed:
   `turnutils_uclient -u '<username>' -w '<credential>' -y turn.example.com`

### 2.5 Backups

`scripts/backup.sh` dumps the database and the photo folder into `backups/` and keeps 7 days
(`KEEP_DAYS` to change). Run it nightly with cron and copy `backups/` off the server:

```bash
crontab -e
30 3 * * * cd /opt/friends && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Restore commands are at the top of the script.

The mobile app lives in `mobile/`; see `mobile/README.md`.

---

## 3. Auth & store setup

- **Google sign-in:** the app sends the Google **ID token**. The server checks it against Google's JWKS
  and requires `aud` to be in `GOOGLE_CLIENT_IDS` (list the Android, iOS and Web client IDs).
- **Apple sign-in:** the app sends `identityToken`, checked against Apple's JWKS with `aud` in
  `APPLE_AUDIENCES`. Apple only sends the user's name on the first sign-in, so pass it as `display_name`.
- **Phone OTP:** `OTP_PROVIDER=console` writes codes to the API log. To use a real SMS gateway, implement
  `OTPProvider.send()` in `app/services/otp.py`, register it in `PROVIDERS`, and set `OTP_PROVIDER`.
- **Google Play:** create a service account with access to your app in Play Console (*Users and
  permissions*). Save its JSON as `secrets/google-play-sa.json` and set `GOOGLE_PLAY_PACKAGE_NAME`.
  The server calls `purchases.subscriptionsv2` and acknowledges new purchases.
- **App Store:** in App Store Connect → *Users and Access → Integrations → In-App Purchase*, create an
  API key. Save the `.p8` file in `secrets/` and set `APPLE_ISSUER_ID`, `APPLE_KEY_ID` and
  `APPLE_BUNDLE_ID`. The client sends the StoreKit transaction ID. The server asks the App Store Server
  API for the subscription status, trying production first and sandbox second.
- Store product IDs and prices live in the `plans` table, editable via `PATCH /admin/plans/{code}`.
  The defaults are `friends_premium_{week,month,year}` (Play) and `friends.premium.{week,month,year}`
  (App Store).
- **Renewals and expiry:** a job expires lapsed subscriptions every minute. Another re-checks
  auto-renewing subscriptions with the store an hour before they expire, so renewals extend premium
  without store webhooks.

---

## 4. API overview

Interactive docs: `/docs`. Every error has the same shape:
`{"error": {"code": "...", "message": "...", "details": ...}}`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/google`, `/auth/apple`, `/auth/otp/request`, `/auth/otp/verify`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all` |
| Linked accounts | `GET /auth/linked-accounts`, `POST /auth/link/{google,apple,phone}`, `DELETE /auth/link/{provider}` |
| Profile | `GET/PATCH/DELETE /me` (DELETE = hard delete), `POST/DELETE /me/photo`, `PUT /me/onboarding` |
| Matching | `GET /turn-credentials`, `WS /ws` |
| Safety | `POST /reports`, `GET /reports/mine`, `POST /blocks`, `GET /blocks`, `DELETE /blocks/{user_id}` |
| Premium | `GET /premium/plans`, `GET /premium/status`, `POST /premium/verify/google`, `POST /premium/verify/apple`, `POST /premium/restore` |
| Public | `GET /health`, `/online-count`, `/safety-tips`, `/meta/interests`, `/meta/countries`, `/meta/languages` |
| Admin | `POST /admin/auth/login`, `GET /admin/me`, `GET /admin/overview`, `GET /admin/stats/calls?days=14\|30\|90`, `GET /admin/stats/top-countries`, `GET /admin/stats/online`, `GET /admin/countries`, `GET /admin/users[?q=&banned=&premium=&country=]`, `GET /admin/users/{id}`, `POST /admin/users/{id}/ban\|unban\|premium`, `DELETE /admin/users/{id}/premium`, `GET /admin/reports[?status=]`, `GET /admin/reports/{id}`, `POST /admin/reports/{id}/review`, `GET/PUT /admin/settings`, `GET /admin/plans`, `PATCH /admin/plans/{code}`, `GET/POST /admin/admins` |

**Signup:** the first sign-in with any provider must include `"confirm_18_plus": true, "accept_terms": true`.
Otherwise the server returns `400 consent_required`, so the app should show the consent checkboxes and
retry. The server stores both timestamps and the terms version.

**Onboarding:** after sign-in `onboarding_step` is 2 (the "step 2 of 3" profile setup screen). Saving a
complete profile (name, age 18+, country, gender) moves it to 3. `PUT /me/onboarding
{"step":3,"completed":true}` finishes onboarding.

**Admin roles:** `analyst` can read stats. `moderator` adds users, bans and reports. `admin` adds
premium, settings and plan prices. `superadmin` adds managing admins.

**Runtime settings** (`PUT /admin/settings`): `gender_filter_enabled`, `country_filter_enabled`,
`priority_matching_enabled`, `chat_enabled`, `maintenance_mode`, `report_threshold`,
`report_window_hours`, `auto_ban_hours`, `recent_match_minutes`, `supported_languages`.

---

## 5. WebSocket protocol (`/ws`)

Connect to `wss://api.example.com/ws?platform=android|ios|web`.
- React Native: pass the access token in an `Authorization: Bearer …` header.
- Browsers: use `?token=<access>`, since browsers can't set WebSocket headers. Caddy redacts it from logs.

Send `{"type":"ping"}` every `heartbeat_seconds`. The server closes idle sockets.

| Client → server | Payload |
|---|---|
| `join_queue` | `{"filters": {"country": "US"\|"Anywhere", "gender": "female"\|"Anywhere"}}`. Filters only apply to premium users |
| `leave_queue` | — |
| `offer` / `answer` | `{"sdp": {...RTCSessionDescription}}` |
| `ice_candidate` | `{"candidate": {...RTCIceCandidate}}` |
| `chat` | `{"text": "hi", "client_msg_id": "local-1"}` (max 500 chars, 10 per 10 s) |
| `next` | end the current call and search again with the same filters |
| `leave` | end the call (or stop searching) without requeueing |
| `ping` | heartbeat |

| Server → client | Payload |
|---|---|
| `connected` | `session_id`, `user`, `heartbeat_seconds` |
| `searching` | `filters` (as applied), `filters_ignored`, `premium`, `timeout_seconds` |
| `matched` | `room_id`, `role` (`caller` creates the offer), `partner {id, display_name, age, country, photo_url}`, `shared_interests` (for "You both like Music") |
| `offer` / `answer` / `ice_candidate` | relayed from the partner, with `room_id` |
| `chat` / `chat_ack` | `id`, `text`, `ts`, `from:"partner"` / `client_msg_id` |
| `partner_left` | `room_id`, `reason` (`left` / `disconnected`; reports and blocks are never revealed), `duration_seconds` |
| `call_ended` | sent to the user who ended the call: `reason`, `duration_seconds` |
| `no_users_found` | nobody compatible within `SEARCH_TIMEOUT_SECONDS`; the client may retry |
| `left_queue`, `pong`, `error {code, message}` | — |

Close codes: `4401` bad token, `4403` banned, `4408` idle, `4409` replaced by a newer session (only the
newest connection per user is kept), `4429` flooding.

**Matching rules:** both users' filters must match each other. Premium users are matched first. Users are
never matched with themselves, with anyone they blocked or who blocked them (checked at join time),
with banned users, or with someone they were matched with in the last `recent_match_minutes`. The call
timer starts when the first `answer` is relayed. Each room's matched, connected and ended times and its
duration are stored in `call_rooms`.

**Moderation:** `POST /reports {room_id, reason, block?}` ends the call. The reported user only sees
`partner_left`. Reports from `report_threshold` different users within `report_window_hours` trigger an
automatic `auto_ban_hours` ban, which kicks the user's live socket. Admins can ban for a set number of
hours or permanently. If a banned user deletes their account, a hash of their login identity is kept
so they can't sign up again until the ban ends.
