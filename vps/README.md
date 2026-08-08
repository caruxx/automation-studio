# Automation Studio VPS

Phase 1 provides the standalone FastAPI and PostgreSQL control-plane base. It includes JWT cookie authentication, TOTP enrollment and verification, one-time recovery-code login, login rate limiting and lockout, invitation-only registration, administrative user listing and disabling, and token encryption primitives.

Phase 2 adds first-class YouTube channels, per-request channel resolution, access filtering through `User.accessible_channel_ids`, administrative channel management, and a one-way importer for the existing shared-drive channel configuration. OAuth credential columns and encrypted accessors are reserved for phase 3; phase 2 does not populate credentials.

Worker tokens, the job queue, and Mac worker integration belong to later phases and are not included here.

## Channel selection

Authenticated requests that depend on a channel resolve it in this order: `X-Channel-Id`, the `channel_id` query parameter, the `as_studio_channel_id` cookie, then the configured default channel. Explicit values must be positive integer database IDs.

## Import existing channels

Apply the Alembic migration first, then run the importer from the `vps` directory. The command only reads the source JSON. It stores a relative folder path only when the source folder contains `/共有ドライブ/`.

```bash
alembic upgrade head
python3 scripts/import_channels.py --source /absolute/path/to/config/channels.json --dry-run
python3 scripts/import_channels.py --source /absolute/path/to/config/channels.json
```

Rows are upserted by the JSON `id`, which maps to `channel_key`. Existing rows only update `name`, `handle`, `youtube_channel_id`, `prefix`, and `folder_rel`.

## Start

```bash
cp .env.example .env
```

Replace every dummy secret in `.env`. Production startup rejects `SECRET_KEY` values shorter than 32 characters. Token encryption in production also requires KMS data-key settings.

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/api/health
```

Both PostgreSQL and the backend bind only to `127.0.0.1`. Publish the backend through a separately configured HTTPS reverse proxy.

## MFA enrollment

When MFA is required, a successful password login for a user without TOTP returns a token valid for 10 minutes, sets `X-MFA-Setup-Required: true`, and limits that token to these endpoints:

- `POST /api/auth/totp/setup` returns a new base32 secret and an `otpauth://` URI.
- `POST /api/auth/totp/verify` accepts a six-digit code, enables TOTP, and returns eight recovery codes once. Store them securely; only their password hashes are retained by the server.

After verification, log in again with `totp_code` to receive a full token. `POST /api/auth/totp/recover` accepts `email`, `password`, and `recovery_code`; each recovery code is invalidated after one successful use.

## Test

Tests use SQLite in memory and require no network access.

```bash
python3 -m pytest tests/ -q
```
