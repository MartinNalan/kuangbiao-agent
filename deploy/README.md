# Cloud Deployment

The current single-server deployment uses:

- Nginx on public port 80.
- QA API and web app on `127.0.0.1:18080`.
- Private KB service on `127.0.0.1:18081`.
- Redis for rate limiting.
- SQLite application and KB databases under `/opt/geowiki/data/`.

The Nginx configuration explicitly returns 404 for `/knowledge/*`.

The application directory is `/opt/geowiki`. The retained Linux service user
`kuangbiao` and systemd unit names `kuangbiao-api.service` /
`kuangbiao-kb.service` are compatibility identifiers, not old application
paths. Do not migrate those identities merely to rename the project.

## T094 v4 production cutover

The production deployment workflow targets
`v4-hybrid-fixed20-p1fix-t094-v1` at
`data/knowledge_base_v4/runtime_private/hybrid_fixed20_t094_v1/runtime_manifest.json`.
T094 is an implementation-only extraction: it reuses the seven accepted T092
private retrieval assets byte-for-byte and keeps
`TECHNICAL_SUFFICIENCY_DECISION_VERSION=t092`. It does not rebuild the corpus,
retrieval units, FTS database or document vectors.

After the T094 Manifest has been built and independently accepted, fill the
ignored `.cloud.env` and run:

```bash
bash scripts/deploy_v4_cloud.sh deploy
```

This workflow preserves remote secrets, account/provider settings and the live
`data/app/application.sqlite`, creates timestamped consistent SQLite and code
rollback snapshots before any upload, and pins the T094 runtime and shared
`TechnicalSufficiencyDecision` version as one fail-closed pair. It transfers
only Git-tracked application code plus the explicit T094 Manifest and seven
hash-pinned private assets. Before upload and again before remote service start,
it recomputes and verifies `runtime_sources.python_import_closure`: the declared
file count, every file SHA-256, bundle and closure hashes must match; the
production closure must contain zero `scripts/` files and must not reach the
T090 or T092 Store. Every local closure file must already be tracked by Git.
The workflow preloads the T094 Store, then starts the private KB service before
the public API. It also preserves and rechecks the separately hosted DeepTutor
service. It does not upload raw PDFs, OCR sources, `.cloud.env`, Git metadata,
Gold fixtures, evaluation reports or other private source-evidence directories.

The GeoWiki cloud virtual environment currently uses Python 3.10. The tracked
`src/sitecustomize.py` supplies only the standard-library `enum.StrEnum`
behavior missing before Python 3.11; it is a no-op on newer interpreters. Both
systemd units set `PYTHONPATH=/opt/geowiki/src`, so this compatibility hook is
loaded before the hash-pinned application modules without changing retrieval
or Decision semantics.

The mutable candidate-staging endpoint uses its own v4-only SQLite file. The
old v3 database is retained on disk solely for rollback and is not opened by
the active KB process.

To restore a recorded pre-cutover state, use the exact backup ID printed by
the deployment command:

```bash
bash scripts/deploy_v4_cloud.sh rollback BACKUP_ID
```

Rollback restores the saved service environment, code, systemd units and the
previous hash-pinned v4 corpus before restarting the services. It deliberately
does not overwrite the live application or candidate database, because doing
so could discard registrations, account activity or candidate review work
created after the backup; consistent point-in-time copies are retained inside
the rollback directory for explicit disaster recovery.

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
