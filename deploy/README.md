# Cloud Deployment

The current single-server deployment uses:

- Nginx on public port 80.
- QA API and web app on `127.0.0.1:18080`.
- Private KB service on `127.0.0.1:18081`.
- Redis for rate limiting.
- SQLite application and KB databases under `/opt/geowiki/data/`.

The Nginx configuration explicitly returns 404 for `/knowledge/*`.

## Current v4 production cutover

The production knowledge service now runs the hash-pinned v4 runtime. For an
in-place update of that runtime, fill the ignored `.cloud.env` and run:

```bash
bash scripts/deploy_v4_cloud.sh deploy
```

This workflow preserves the remote `.env` values and
`data/app/application.sqlite`, creates a timestamped rollback point before any
upload, transfers only code and the explicit private v4 runtime assets,
verifies every manifest hash remotely, preloads the v4 store before changing
the service environment, and then restarts the KB service before the public
API. It does not upload raw PDFs, OCR sources, `.cloud.env`, Git metadata or
other private source-evidence directories.

The mutable candidate-staging endpoint uses its own v4-only SQLite file. The
old v3 database is retained on disk solely for rollback and is not opened by
the active KB process.

To restore a recorded pre-cutover state, use the exact backup ID printed by
the deployment command:

```bash
bash scripts/deploy_v4_cloud.sh rollback BACKUP_ID
```

Rollback restores the saved service environment, code and systemd units and
reactivates the retained v3 files. It deliberately does not overwrite the live
application database, because doing so could discard registrations or account
activity created after the backup; a point-in-time application-database copy
is kept inside the rollback directory for explicit disaster recovery.

## Legacy v3 bootstrap

`scripts/sync_cloud.sh` is the legacy v3 bootstrap/synchronization workflow. It
copies the old v3 database and ANN files and replaces the remote `.env`; using
it against the current v4 production instance can reactivate v3. Do not use it
for routine v4 deployment.

For a deliberately authorized legacy v3 bootstrap only, fill `.cloud.env`,
keep it outside Git, then run:

```bash
bash scripts/sync_cloud.sh
```

The legacy script synchronizes code, the v3 private knowledge database, and the local runtime `.env`, then installs system dependencies and systemd services. It also uses the configured AgentMail token on the server to create or reuse the `geowiki` registration inbox. It does not upload `.git`, `.venv`, logs, PDFs, Office files, or other `data/` content.

When deploying code while the KB specialist is rebuilding the local private database, skip only the DB transfer:

```bash
SYNC_KB_DB=false bash scripts/sync_cloud.sh
```

This still synchronizes application code and runtime configuration and restarts the services, while preserving the current cloud KB file.

If the server cannot reach AgentMail, deployment still completes but sets `REGISTRATION_ENABLED=false`. Existing users can continue to log in and use API Keys; new registration remains closed until the email provider is reachable. Verification codes are never exposed as a production fallback.

When AgentMail requires a dedicated overseas egress path, set `CLOUD_AGENTMAIL_PROXY_URL=socks5://127.0.0.1:19090` in the ignored `.cloud.env`. The API passes only AgentMail requests to this proxy through `AGENTMAIL_PROXY_URL`; model, embedding, knowledge-base, and other HTTP clients continue to connect directly. A hardened reusable unit is provided at `deploy/systemd/geowiki-agentmail-tunnel.service`; its SSH key and `/etc/geowiki-agentmail-tunnel/tunnel.env` remain server-local and must never be committed.

Before deployment, set `AGENTMAIL_API_KEY` in the ignored local `.env` and prepare the remaining verification settings without contacting AgentMail:

```bash
PYTHONPATH=src .venv/bin/python scripts/setup_agentmail.py --defer-inbox
```

After deployment, allow TCP port 80 in the cloud security group and visit the server IP.

## Initial Admin

Create the first administrator directly on the server:

```bash
cd /opt/geowiki
sudo -u kuangbiao env PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py create-admin \
  --account admin --display-name 管理员
```

For automated bootstrap, append `--generate-password`; the temporary password is displayed only in that command output and should be changed after the first login.

Then create an invitation:

```bash
sudo -u kuangbiao env PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py create-invite \
  --label "第一轮内测" --admin-account admin
```

Plaintext invitation codes and API keys are displayed only once.

## Add a Domain and HTTPS

Replace `server_name _;` with the domain, install Certbot, and issue a certificate. After HTTPS works, set:

```text
PUBLIC_BASE_URL=https://your-domain.example
SESSION_COOKIE_SECURE=true
```

Restart the API after environment changes:

```bash
sudo systemctl restart kuangbiao-api
```

Do not expose ports 18080 or 18081 in the cloud security group. Only 80/443 and restricted SSH access should be public.
