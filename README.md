# Suraksha SOS backend

FastAPI service for authenticated SOS intake, nearby responder queries, media, and live updates.

## Run

Use Python 3.11+ and PostgreSQL with PostGIS available for the historical initial migration.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set DATABASE_URL and a randomly generated JWT_SECRET_KEY (at least 32 characters).
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`GET /health` is a process liveness check, not a database readiness guarantee. Protect deployments with HTTPS and restrict CORS to your client origins. Uploaded media lives in `uploads/`; mount persistent storage there.

## Authentication contract

An external identity provider must issue signed JWTs. This repository does **not** implement account registration/login or issue production tokens. Tokens must contain `sub` (user ID), `exp`, `iss=suraksha`, and `aud=suraksha-api` (issuer/audience are configurable). The default signing algorithm is HS256. Never embed the signing key in a mobile app or use an `EXPO_PUBLIC_*` variable for it.

All SOS endpoints require a bearer token. Direct creates and cancellations must match `sub`; cancellation and upload also verify event ownership. Nearby queries, acknowledge endpoint, media belonging to other users, and the WebSocket require `role=responder` or `role=admin`. Any authenticated phone may relay an SOS with a valid P-256 origin signature and backend-issued device certificate. Enrollment is `POST /api/v1/devices/enroll`; identity is `GET /api/v1/identity/me`. The `/respond` action is available to authenticated nearby volunteers; dashboard queries and acknowledgement remain responder-only.

WebSocket URL: `/api/v1/sos/ws/sos?client_id=<unique-id>`, with the bearer token in the upgrade Authorization header. Browser clients need a trusted proxy supplying that header or a future short-lived WebSocket ticket integration; credentials are deliberately not accepted in query strings. Connections close when their JWT expires.

## API contract

Request keys use snake_case; coordinates use `location.lat` and `location.lng`. `client_timestamp` must include a timezone. TTL is 1–3600 seconds, measured from that timestamp, and relay hops cannot exceed 15. The mobile repository converts its camelCase objects at the API boundary.

- `POST /api/v1/sos`: submit an alert, idempotent by `sos_id`.
- `POST /api/v1/sos/cancel`: `{sos_id, user_id, reason?}`. A cancellation received before its alert records a tombstone so delayed relays cannot resurrect it.
- `GET /api/v1/sos/active?lat=...&lng=...&radius_km=...`: active, acknowledged, and responding alerts inside the radius, excluding expired alerts.
- `POST /api/v1/sos/{id}/acknowledge` and `/respond`: form field `user_id`; repeat responder requests are idempotent.
- `POST /api/v1/sos/media`: multipart fields `sos_id`, `uploaded_by`, and `file`. Supported MIME types: JPEG, PNG, MP3, M4A, MP4; default maximum is 10 MiB. MIME type is client-declared; content inspection/malware scanning is a deployment concern.
- `GET /api/v1/sos/media/{attachment_id}`: authenticated media download.

## Existing databases

Back up the database before upgrading. Revision 002 converts historical PostGIS coordinates to the latitude/longitude fields used by the application and preserves the coordinates. Revision 003 records cancellations. Normal startup no longer silently creates or changes schemas. `AUTO_CREATE_TABLES=true` is only for a new, disposable development database.

If the previous application already created tables through `create_all` and there is no Alembic version, first verify those tables match the previous repository models, then explicitly baseline and upgrade:

```sh
alembic stamp 001_create_sos_table
alembic upgrade head
```

Do not stamp an empty or unrelated database. Revision 002 handles both the original PostGIS migration schema and the previous application-created coordinate schema. Its downgrade refuses to discard responder history; restore a backup instead. Old media URLs under `/static` are no longer anonymously exposed; migrate existing media storage references before serving historical attachments.

## Signed relay and WebGIS integration

Revision 004 adds a generated PostGIS geography point and GiST index; nearby queries use ST_DWithin/ST_Distance. Latitude/longitude remain the API contract. Run migrations, not create_all, in production.

With `REDIS_ENABLED=true`, each alert/status mutation writes a transactional database outbox; workers publish committed events to Redis and all API processes fan them out to their WebSockets. Delivery is at least once. WebGIS must upsert by sos_id and reconcile `/active` on connect/reconnect because Redis Pub/Sub and WebSockets are not durable client queues. With Redis disabled, fanout and rate limiting are single-process only.

Device certificates bind a P-256 public key to the authenticated subject and device ID. Relayed origin identity, location, timestamp, TTL, hop limit and message are verified before persistence. Mutable relay history is telemetry, not authenticated evidence of a route. Accuracy/altitude/media metadata are not origin-signed. Offline certificates expire after 30 days by default; offline revocation is not possible. Keep the CA private key stable in a secret manager and arrange certificate renewal and coordinated CA pin rotation. Production OIDC uses configurable issuer/audience/algorithm and a PEM verification key; key rotation must update backend configuration. No production identity provider or credentials are included.

See [TESTING.md](TESTING.md) for Docker setup, test accounts, database checks and phone acceptance tests. [examples/active-relayed-sos.json](examples/active-relayed-sos.json) is a synthetic response fixture; it is not a signed intake request.
