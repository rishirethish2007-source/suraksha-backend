# SOS setup and testing

Use disposable test data. BLE testing requires two physical phones (three for multiple hops), Bluetooth enabled and the native app installed. Location/notification permission prompts must be accepted explicitly. Do not confuse a successful API upload with a rescue dispatch.

## 1. Backend

From `suraksha-backend`, with Python 3.11+ and Docker Compose:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/dev_setup.py
docker compose up --build
```

The setup script refuses to overwrite `.env`. It generates a random JWT secret, P-256 certificate authority and `dev-tokens.json` containing separate phone-a, phone-b and phone-c responder tokens valid for 24 hours. Keep these files local. For a fresh test reset, back up/move those local files then rerun setup; do not rotate a running production CA this way. Docker applies migrations and starts PostGIS, Redis and the API at port 8000. Open `http://localhost:8000/docs`; `/health` is liveness only. Use the Swagger Authorize header or curl with your test bearer token. Mobile uses the laptop's reachable hostname/IP, not localhost.

For existing databases, back up before `alembic upgrade head`; see backend README for historical baselining. Docker credentials are for local tests only.

## 2. Install the native mobile app

From `suraksha-mobile`:

```sh
npm ci
cp .env.example .env
# Edit .env: reachable HTTPS API URL; enable EXPO_PUBLIC_ENABLE_TEST_LOGIN=true for this test.
npx expo run:android --device
# On macOS with Xcode and a configured signing team:
npx expo run:ios --device
```

Alternatively use the existing EAS development profile: `npx eas-cli@latest build --profile development --platform android` (or ios). This needs your own Expo account/project access, signing credentials, and may use paid build quota. Configure the same environment values in that build environment. A preview build (`--profile preview`) embeds JS and is preferable for tests disconnected from Metro. For development builds keep Metro reachable or load JS before disconnecting; Metro failure is not a BLE failure.

Prefer an HTTPS development endpoint reachable by all phones. For Android USB testing only, set API URL to `http://127.0.0.1:8000`, set `EXPO_PUBLIC_ALLOW_INSECURE_HTTP=true` and use `adb reverse tcp:8000 tcp:8000` for each connected device. Debug builds permit development traffic; release builds may still reject cleartext. Never enable these test settings in production. Restart Metro after changing EXPO_PUBLIC values; rebuild embedded bundles when testing preview builds.

While all phones are online, paste phone-a's token into A's test login, phone-b's into B, etc., and tap **Connect test account and enroll device**. Enrollment is required before going offline. Enable nearby relay on every phone while the app is foregrounded. Confirm the Android persistent notification and allow Bluetooth, precise foreground location and local notifications.

Production login: set EXPO_PUBLIC_OIDC_ISSUER/CLIENT_ID instead of test login. Register `suraksha://auth-callback`; configure the provider's access-token audience and backend JWT_PUBLIC_KEY/JWT_ALGORITHM/JWT_ISSUER/JWT_AUDIENCE. The repository does not provision that external identity provider.

## 3. Acceptance matrix

| Test | Procedure | Expected evidence |
|---|---|---|
| Direct | A online, trigger SOS outdoors | UI reports online delivery; backend row has A's ID, coordinates, hop_count=0 |
| One hop | Disable Wi-Fi and cellular on A, keep Bluetooth on; B online | B shows nearby alert, coordinates/distance when available; API receives A's ID with hop_count>=1 and relay chain |
| Multi hop | A and B offline, C online; keep A out of C's radio range | B stores/rebroadcasts; C uploads; route includes B and C. Confirm A cannot reach C directly |
| Backend failure | Stop only API while B has internet | Queue is retained and BLE forwarding continues; restart API and observe upload |
| Restart | Queue an alert offline, background/reopen app before TTL | Queued alert survives; retry uploads once online; one database row per sos_id |
| Background | Enable relay first, lock B for several minutes, trigger from A | Android notification/service and relay timing observed; record iOS delivery behavior without assuming guarantees |
| Multiple alerts | Trigger two distinct SOS events offline | Both eventually relay through rotating advertisements; no cross-packet mixing |
| Offer help | B online, open received alert and tap I can help twice | Backend responders_en_route increases only once for B |
| Cancel | Cancel on A; reconnect it if offline | Cancellation queued then applied; late duplicate uploads cannot reactivate it; remote offline UI can stay stale until TTL |
| TTL / hops | Run unit tests; use a short signed TTL in a test build | Expired packets are not notified/uploaded; no forwarding beyond maxHops |
| Forgery / replay | Run signature tests and resend same signed event | Forged origin fails; replay returns is_duplicate=true without a second event |
| Permissions / radio | Deny location, disable Bluetooth, revoke notifications | Clear error/queued state; no false server-delivery claim; denied notifications do not block relay |
| Platform limits | Repeat Android↔Android, iOS↔iOS, both mixed directions, force-quit and battery saver | Record actual supported behavior; iOS background advertisements may be invisible to Android |

Inspect active events using a responder token:

```sh
curl -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/api/v1/sos/active?lat=12.9716&lng=77.5946&radius_km=50'
```

Use coordinates near the test phone. The synthetic example is not geographically near every tester. WebGIS receives `{type: new_sos|sos_update|sos_cancel, data: ...}` at `/api/v1/sos/ws/sos?client_id=unique`. Native clients can provide an Authorization header; browser WebGIS needs its common authenticated proxy to supply that header. Upsert by sos_id and call `/active` after every reconnect. Shared Redis fanout requires REDIS_ENABLED=true on every API worker.

## 4. Automated checks

Backend:

```sh
python -m pytest -q
# After docker compose has created the schema, exercise real PostGIS/Redis:
TEST_DATABASE_URL=postgresql+asyncpg://suraksha:local-test-only@127.0.0.1:5432/suraksha TEST_REDIS_URL=redis://127.0.0.1:6379/0 python -m pytest -q tests/test_postgis.py
```

Use a disposable database; the integration test creates and removes its own UUID event. Stop the API publisher while specifically asserting pending-outbox contents (`docker compose stop api`); db/redis stay running.

Mobile:

```sh
npm run typecheck
npm run lint
npm test
npx expo export --platform all
```

Tests cover authentication/ownership, deduplication, cancellations, TTL, spatial lookup, outbox publication, packet limits, signature tampering and durable forwarding. CI additionally compiles Android native code; successful JS tests/export do not prove native compilation or radio interoperability. Physical radio/background behavior and production OIDC/CA configuration remain deployment acceptance gates.

## Fix identity/me HTTP 503 during local test sign-in

The API returns `Authentication is not configured` if there is no JWT public key and the configured JWT secret is shorter than 32 characters. Passing unit tests does not configure the running server: tests use temporary keys.

Stop Uvicorn and run `python scripts/dev_setup.py --repair-local-auth` from the backend root. This preserves database settings, custom issuer/audience, valid JWT secrets and valid device CA keys; it backs up `.env` before repairing missing/short local auth settings and generates fresh 24-hour tokens. It refuses to replace an external JWT provider or conflicting process-environment overrides. Restart Uvicorn explicitly (editing `.env` may not trigger the reload watcher), then use the new phone-a token from `dev-tokens.json`. Do not post tokens or `.env` contents publicly.

This addresses authentication configuration only. The skipped PostGIS test is separate: set TEST_DATABASE_URL to a disposable migrated PostGIS database to run it.
