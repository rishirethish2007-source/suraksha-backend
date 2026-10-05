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

## Responder dashboard and directions

The backend now serves `/dashboard` directly; no new APK or Node build is required.
After updating the backend files, keep your existing `.env` and database, then
restart Uvicorn. Open `http://localhost:8000/dashboard` on the laptop (or the
laptop's LAN IP on another device). Paste the `phone-c` token from `dev-tokens.json`
for local testing. Only responder/admin tokens can retrieve dashboard incidents.
Token storage is in tab memory only. If the token expired, run the existing
`python scripts/dev_setup.py --repair-local-auth` command to issue fresh tokens.

1. Connect the dashboard, then trigger an SOS from Phone A, directly or via B.
2. Within the next 15-second refresh, select the received alert. Verify its name,
   phone if supplied, message, coordinates, GPS accuracy, timestamps and relay hops.
3. Click Show location map, or Open location. The embedded map uses OpenStreetMap;
   the external location and directions links use Google Maps. They need internet.
4. Set the rescue team's origin coordinates, or choose Use my location on the
   responder's device. Browser geolocation requires permission and localhost or
   HTTPS; a plain HTTP LAN-IP page may require manual coordinates instead.
5. Choose Driving or Walking and click Directions & ETA. Google Maps calculates
   route choices and travel-time estimates. With no origin entered, Maps asks for
   the starting point or uses its device location. A dispatch laptop's location
   is not necessarily the rescue team's location.
6. Acknowledge the event or Mark me en route. Repeating these actions is idempotent.
7. Cancel the SOS on A. Once cancellation reaches the backend, the card disappears
   on the next refresh. Expired events also disappear. Newest-first pages contain
   50 alerts; Next/Previous navigate older active alerts.

`GET /api/v1/dashboard/events?offset=0&limit=50` is authenticated, role-restricted,
paginated, excludes terminal/expired alerts, and returns no-store responses. The
public HTML shell contains no incident data. Dashboard polling pauses in a hidden
tab; it does not call `/health`. This dashboard does not change the phone retry
behavior discussed previously.

Directions hand off to Google's route provider; this module does not calculate
or certify a globally fastest/safe route, ingest live disaster road closures, or
supply offline navigation. Responders must verify local access conditions.

## Individual sign-in and saved server settings (mobile 1.1.0)

Update backend code, keeping `.env`, uploads, signing keys and database volumes.
With your normal virtual environment active and DATABASE_URL pointing to the
running PostGIS database, stop Uvicorn and run:

```powershell
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Migration 005 adds accounts and account_sessions without replacing SOS data.
The existing JWT_SECRET_KEY must contain at least 32 characters and the local
signing algorithm must be HS256. The existing dev_setup repair command can
configure missing local keys; do not overwrite a working .env. External OIDC
configurations continue using organisation sign-in instead of this local issuer.

1. Install APK 1.1.0 over Suraksha Test on both phones.
2. Open **Settings — backend address**, enter `http://172.18.66.69:8000` (or the
   laptop's current address) and tap **Check connection and save**. Settings remain
   after restarting the app. This checks the device CA without sending credentials.
3. Sign out of any old phone-a/phone-b test session. Choose **New user? Create an
   account**, supply a unique email, password of 12–128 characters, name and phone.
   Each person uses their own account. Email is a login identifier, not verified.
4. Close/reopen the app, then test again after 15 minutes: access renews automatically.
   Device certificates renew on SOS generation when nearing expiry and online.
5. Change the laptop IP and repeat step 2. The same backend CA preserves sign-in.
   An unrelated CA is rejected to prevent accidentally sending credentials/queued
   alerts to another organisation. A genuinely different backend needs fresh app
   setup; changing an IP does not require an APK rebuild.

Access JWTs last 15 minutes. Random per-device refresh secrets have no scheduled
expiry, are saved in SecureStore on the phone, and are stored only as SHA-256
hashes on the backend. Logout revokes the refresh secret when online and always
clears local credentials. If logout occurs offline, an operator can revoke the
remaining server session. Issued access JWTs remain valid for at most 15 minutes.
This provides persistent sign-in, not an irrevocable permanent access token.

New accounts always have the user role. To give a registered rescue worker
responder-dashboard access, run on the trusted backend machine:

```powershell
python scripts/manage_account.py responder@example.com --role responder
```

Then use the dashboard's email/password sign-in. Role changes revoke refresh
sessions; sign in again. Dashboard credentials stay in tab memory, while mobile
sign-in persists. Operator commands also support `--revoke-sessions`, `--disable`,
`--enable`, and `--reset-password` (password entered via a hidden prompt).

Local HTTP is explicitly a test-build feature. Deploy account sign-in over HTTPS.
These local accounts do not include email verification or email-based password
recovery; use the trusted operator password-reset command or organisation OIDC.
The existing test-token path remains under Advanced for compatibility, and those
old test tokens still expire after 24 hours.
