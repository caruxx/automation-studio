# Automation Studio VPS

Phase 1 provides the standalone FastAPI and PostgreSQL control-plane base. It includes JWT cookie authentication, TOTP enrollment and verification, one-time recovery-code login, login rate limiting and lockout, invitation-only registration, administrative user listing and disabling, and token encryption primitives.

Phase 2 adds first-class YouTube channels, per-request channel resolution, access filtering through `User.accessible_channel_ids`, administrative channel management, and a one-way importer for the existing shared-drive channel configuration. OAuth credential columns and encrypted accessors are reserved for phase 3; phase 2 does not populate credentials.

Phase 3 adds a VPS-hosted YouTube OAuth web flow. OAuth client secrets and refresh tokens are encrypted at rest. Access tokens are refreshed on demand and cached only in process memory. Existing `.youtube_token.json` files are not imported because refresh tokens are bound to the OAuth client that issued them.

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

Replace every dummy secret in `.env`. Production startup rejects `SECRET_KEY` values shorter than 32 characters. Production token encryption requires an AWS KMS data-key keyring, a local keyring file, or a directly configured local keyring.

The recommended production setup is a root-owned local keyring file outside the repository and any shared drive:

```bash
sudo install -d -m 0700 /etc/automation-studio
sudo python3 scripts/generate_keyring.py --out /etc/automation-studio/keyring.json
```

Set `TOKEN_LOCAL_KEYRING_FILE=/etc/automation-studio/keyring.json` and set `TOKEN_ACTIVE_KEY_ID` to the generated key ID. Docker Compose mounts `/etc/automation-studio` read-only in the backend container. Never store the keyring inside this repository or a shared drive. Production refuses a keyring file readable by group or other users.

Back up the keyring because losing it makes saved refresh tokens impossible to decrypt. Store the keyring backup and the database backup in separate locations.

For key rotation, add a new `key_id` and key to the JSON object, then change `TOKEN_ACTIVE_KEY_ID` to the new ID. Existing envelopes retain their `key_id`, so keep old keys in the keyring until all values using them have been replaced.

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/api/health
```

Both PostgreSQL and the backend bind only to `127.0.0.1`. Publish the backend through a separately configured HTTPS reverse proxy.

## YouTube OAuth setup

Complete these manual steps in Google Cloud Console before starting authorization:

1. Select or create the Google Cloud project used for this VPS and enable YouTube Data API v3 and YouTube Analytics API.
2. Configure the OAuth consent screen and add the Google accounts that may authorize channels while the app remains in testing mode.
3. Open APIs and Services, Credentials, then create an OAuth client ID with application type `Web application`.
4. Add the public HTTPS callback URL as an authorized redirect URI. It must exactly match `YOUTUBE_OAUTH_REDIRECT_URI`, including scheme, host, path, and trailing-slash behavior. The expected path is `/api/oauth/callback`, for example `https://studio.example.com/api/oauth/callback`.
5. Set `YOUTUBE_OAUTH_REDIRECT_URI` in `.env`, then restart the backend.
6. As an administrator, save the web client ID and client secret with `PUT /api/channels/{channel_id}/oauth-client`. This operation does not return either value and clears any refresh token previously issued for a different client.
7. While authenticated as a user with access to that channel, call `POST /api/channels/{channel_id}/oauth/start` and open the returned `authorization_url`. Google redirects back to the VPS callback after consent.

The authorization request asks for these scopes:

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube`
- `https://www.googleapis.com/auth/yt-analytics.readonly`

Use `GET /api/channels/{channel_id}/oauth/status` to check whether authorization is present. It returns only `has_credentials` and `credentials_updated_at`. `DELETE /api/channels/{channel_id}/oauth` is administrator-only and removes the stored refresh token without removing the OAuth client configuration.

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
