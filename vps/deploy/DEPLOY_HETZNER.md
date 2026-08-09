# Hetzner + Cloudflare production deployment

Target hostname: `yt.caruvistar.jp`

Run repository commands from the `vps` directory. The commands below use the production override consistently:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml config
```

## 1. Create the Hetzner server

Create a Hetzner Cloud server with Ubuntu 24.04 and a CPX21-class plan. Any suitable location may be used. Video encoding and `ffmpeg` run on the Mac worker, so the VPS only hosts the control plane, PostgreSQL, and the job API and does not need a high-performance encoding plan.

Add the administrator's SSH public key during creation. Record the server IPv4 address as `SERVER_IP`.

## 2. Harden SSH and firewalls

Log in as root once, create an administrative user, and copy the authorized key:

```bash
adduser deploy
usermod -aG sudo deploy
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys
```

Open a second terminal and confirm `ssh deploy@SERVER_IP` works before disabling root and password login. Create `/etc/ssh/sshd_config.d/99-hardening.conf` with:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

Validate and reload SSH:

```bash
sudo sshd -t
sudo systemctl reload ssh
```

Enable UFW with only SSH, HTTP, and HTTPS exposed:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Also attach a Hetzner Cloud Firewall. Allow TCP 22 only from fixed administrator IP addresses. Allow TCP 80 and 443 from Cloudflare's published IPv4 and IPv6 ranges only, and deny other inbound traffic. Reconcile those ranges with `deploy/nginx/yt.caruvistar.jp.conf` and <https://www.cloudflare.com/ips/> whenever Cloudflare changes them. Keeping the origin unreachable outside Cloudflare is required for trustworthy `CF-Connecting-IP` handling.

Install fail2ban:

```bash
sudo apt update
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status
```

Enable the `sshd` jail in a local file if the packaged defaults do not enable it, then confirm `fail2ban-client status sshd` succeeds.

## 3. Install Docker and the Compose plugin

Install Docker from Docker's Ubuntu repository:

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
sudo systemctl enable --now docker
```

Log out and back in so the `docker` group applies. Confirm `docker version` and `docker compose version`.

## 4. Clone the repository and create `.env`

```bash
sudo install -d -m 0755 -o deploy -g deploy /opt/automation-studio
git clone REPOSITORY_URL /opt/automation-studio/repository
cd /opt/automation-studio/repository/vps
cp .env.example .env
chmod 0600 .env
```

Generate independent random values for `SECRET_KEY` and `POSTGRES_PASSWORD`, and replace every placeholder. Set at least:

```dotenv
APP_ENV=production
CORS_ORIGINS=https://yt.caruvistar.jp
YOUTUBE_OAUTH_REDIRECT_URI=https://yt.caruvistar.jp/api/oauth/callback
TRUSTED_CLIENT_IP_HEADER=cf-connecting-ip
TOKEN_LOCAL_KEYRING_FILE=/etc/automation-studio/keyring.json
TOKEN_ACTIVE_KEY_ID=k1
```

`TRUSTED_CLIENT_IP_HEADER` must not be enabled unless Nginx overwrites `CF-Connecting-IP` and the origin accepts HTTP/HTTPS only from Cloudflare ranges. The supplied Nginx configuration does both header replacement and trusted-proxy validation; the Hetzner firewall supplies the origin restriction.

## 5. Generate and back up the encryption keyring

From the `vps` directory, generate the key outside the repository:

```bash
sudo install -d -m 0700 -o root -g root /etc/automation-studio
sudo python3 scripts/generate_keyring.py --out /etc/automation-studio/keyring.json
sudo chown 10001:10001 /etc/automation-studio/keyring.json
sudo chmod 0600 /etc/automation-studio/keyring.json
sudo stat /etc/automation-studio/keyring.json
```

Confirm the JSON key ID is `k1`, matching `TOKEN_ACTIVE_KEY_ID=k1`. The production override runs the capability-free, read-only backend container as UID 0 solely so it can read this required root-owned 0600 file.

Back up the keyring before authorizing any YouTube channel. Losing it makes all saved OAuth `refresh_token` values permanently undecryptable. Keep an encrypted offline keyring backup and a database backup in separate locations, and restrict access to both.

## 6. Configure Cloudflare and the origin certificate

In Cloudflare DNS, add an `A` record named `yt` pointing to `SERVER_IP` and enable proxying so the cloud is orange. In SSL/TLS settings, select `Full (strict)`.

The origin certificate is issued by Let's Encrypt (adopted 2026-08-09). `Full (strict)`
requires a publicly valid certificate on the origin, and Let's Encrypt renews itself,
so no manual reissue is needed. A Cloudflare Origin Certificate also satisfies
`Full (strict)`; switch the `ssl_certificate` paths in the Nginx configuration if it is
preferred.

Issue the certificate before installing the site configuration. The HTTP-01 challenge
passes through the Cloudflare proxy, so `Always Use HTTPS` must stay off during issuance
(the redirect happens at the edge and never reaches the origin otherwise):

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot certonly --webroot -w /var/www/html -d yt.caruvistar.jp \
  --agree-tos -m <admin-email> --non-interactive --no-eff-email
```

Certbot installs a renewal timer automatically. The port 80 server block keeps
`/.well-known/acme-challenge/` unredirected so renewals keep working.

Install the site configuration:

```bash
sudo cp deploy/nginx/yt.caruvistar.jp.conf /etc/nginx/sites-available/yt.caruvistar.jp.conf
sudo ln -s /etc/nginx/sites-available/yt.caruvistar.jp.conf /etc/nginx/sites-enabled/yt.caruvistar.jp.conf
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

The Nginx config permits a 120-second upstream read timeout. Cloudflare Free returns error 524 at approximately 100 seconds, so every Mac worker long-poll must use a timeout of 90 seconds or less.

## 7. Start Compose and apply migrations

Review the fully merged configuration and verify that backend and PostgreSQL ports bind only to loopback and no application source directory is mounted:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml config
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml build --pull
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d db
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml run --rm backend alembic upgrade head
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps
```

The image entrypoint also runs `alembic upgrade head`; the explicit command above makes the migration a visible deployment gate.

## 8. Create the initial administrator

Run this once. It prompts inside the container so the password is not written into shell history:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec backend python -c 'from getpass import getpass; from app.db import SessionLocal; from app.models.db_models import User; from app.password_policy import assert_password_or_400; from app.security import hash_password; email=input("Admin email: ").strip().lower(); password=getpass("Admin password: "); db=SessionLocal(); existing=db.query(User).filter(User.email==email).first(); assert existing is None, "user already exists"; assert_password_or_400(password, email=email, name="Administrator"); db.add(User(email=email, name="Administrator", role="admin", is_active=True, password_hash=hash_password(password))); db.commit(); db.close(); print("admin created")'
```

Keep `MFA_REQUIRED=true`. The first successful password login returns an MFA setup session rather than a full administrator session.

## 9. Configure Google OAuth manually

In Google Cloud Console:

1. Select or create the production project and enable YouTube Data API v3 and YouTube Analytics API.
2. Configure the OAuth consent screen and its production or test users.
3. Create an OAuth client with application type `Web application`.
4. Add exactly `https://yt.caruvistar.jp/api/oauth/callback` to Authorized redirect URIs.
5. After channel import, sign in as an administrator and save that web client ID and secret for each channel through `PUT /api/channels/{channel_id}/oauth-client` or the application UI.

Do not reuse a desktop OAuth client. Do not put the client secret in Git.

## 10. Verify the deployment

Perform these checks in order:

1. `curl -fsS https://yt.caruvistar.jp/api/health` returns `{"status":"ok"}`.
2. Log in with the initial administrator password. Confirm an account without TOTP receives the setup-required response.
3. Register TOTP, securely store the one-time recovery codes, then log in again with a current TOTP code.
4. Apply migrations and dry-run then execute the channel importer from the container or a controlled checkout. Confirm imported channels are visible and access rules are correct.
5. Save the web OAuth client on a test channel, start OAuth consent, approve it with the intended Google account, and confirm the callback returns to `https://yt.caruvistar.jp/api/oauth/callback`.
6. Confirm the channel OAuth status reports credentials without exposing the refresh token or client secret.
7. Inspect `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs --tail=200 backend` and the Nginx access/error logs for unexpected errors.

Also verify direct origin access is blocked by the Hetzner Cloud Firewall. Requests through Cloudflare should reach Nginx, and Nginx must overwrite both `X-Forwarded-For` and `CF-Connecting-IP` with its validated `$remote_addr` before proxying.

## 11. Verify PostgreSQL prevents duplicate job leases

This concurrency behavior has only been tested locally with SQLite. The following PostgreSQL test is mandatory on production before workers are enabled.

Use the authenticated API to create two worker tokens owned by a user who can access the test channel. Store their one-time plaintext values locally as `WORKER_TOKEN_A` and `WORKER_TOKEN_B`. Obtain a full user access token after TOTP as `ADMIN_TOKEN`, choose an accessible `CHANNEL_ID`, and create exactly one queued job with a unique probe type:

```bash
export API_BASE=https://yt.caruvistar.jp
export PROBE_TYPE=lease_probe_$(date +%s)
JOB_ID=$(curl -fsS -X POST "$API_BASE/api/jobs" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "X-Channel-Id: $CHANNEL_ID" \
  -H "Content-Type: application/json" \
  -d "{\"job_type\":\"$PROBE_TYPE\",\"payload\":{\"purpose\":\"postgres-lease-test\"},\"max_attempts\":3}" | jq -r .id)
```

Make two lease requests concurrently, each with a different worker token:

```bash
RESULT_A=$(mktemp)
RESULT_B=$(mktemp)
curl -fsS -X POST "$API_BASE/api/worker/jobs/lease" \
  -H "Authorization: Bearer $WORKER_TOKEN_A" \
  -H "Content-Type: application/json" \
  -d "{\"job_types\":[\"$PROBE_TYPE\"],\"lease_seconds\":300,\"max_jobs\":1}" >"$RESULT_A" &
PID_A=$!
curl -fsS -X POST "$API_BASE/api/worker/jobs/lease" \
  -H "Authorization: Bearer $WORKER_TOKEN_B" \
  -H "Content-Type: application/json" \
  -d "{\"job_types\":[\"$PROBE_TYPE\"],\"lease_seconds\":300,\"max_jobs\":1}" >"$RESULT_B" &
PID_B=$!
wait "$PID_A" "$PID_B"
jq -s --argjson id "$JOB_ID" '[.[][] | .id] as $ids | {returned_ids:$ids, target_count:([$ids[] | select(. == $id)] | length), unique_count:($ids | unique | length)}' "$RESULT_A" "$RESULT_B"
jq -e -s --argjson id "$JOB_ID" '[.[][] | .id] | length == 1 and .[0] == $id' "$RESULT_A" "$RESULT_B"
```

The final command must exit zero: across both responses the probe job ID appears exactly once, with one response containing the job and the other returning `[]`. Repeat several times with a new `PROBE_TYPE` and job each time. If the same job ID appears twice, stop deployment and do not enable workers. Remove the probe jobs through the application after verification, disable the two temporary worker tokens, unset the shell variables, and delete the temporary result files.

## 12. Roll back

Before every release, record the deployed Git commit, back up PostgreSQL, and verify the encrypted keyring backup:

```bash
mkdir -p /opt/automation-studio/backups
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T db pg_dump -U asstudio -d asstudio -Fc > /opt/automation-studio/backups/asstudio-before-release.dump
git rev-parse HEAD
```

To roll application code back, check out the previously recorded tag or commit, rebuild, and restart:

```bash
git checkout PREVIOUS_KNOWN_GOOD_COMMIT
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml build
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d
curl -fsS https://yt.caruvistar.jp/api/health
```

Do not run a blind Alembic downgrade. If the old application is incompatible with the migrated schema, stop the stack, preserve a copy of the current database, restore the pre-release dump into PostgreSQL, and then start the known-good image. Never replace or regenerate the keyring during rollback; the restored database must be paired with the keyring that encrypted its saved refresh tokens.

For an Nginx-only rollback, restore the previous site file, run `sudo nginx -t`, and reload Nginx. For a failed rollout where the existing containers are still healthy, leave them running until the replacement image and configuration have passed validation.
