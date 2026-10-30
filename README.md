# RawRoute deployment

Linux, Docker Engine, Docker Compose v2 with `up --wait`, and Python 3 are required.
This is a standalone deployment repository. Nothing deploys on clone or test.

## Local source and parallel deployment

`python3 build.py` builds clean committed source from `../rawroute` and prints the
immutable image and deployment command. Pass `--local-image` to `deploy.py` for
these locally built images. Review compatibility before applying updates.

To coexist with another deployment, set `RAWROUTE_SITE=:80`,
`RAWROUTE_HTTP_PORT=8081`, `RAWROUTE_HTTPS_PORT=8443`, and
`RAWROUTE_PUBLIC_URL=http://your-server:8081` in `.env`. The default remains HTTPS
on ports 80/443. Host port 8443 is reserved but does not serve HTTPS in HTTP mode.
Persist imported runtime overrides/secrets in private `.state/runtime.env`
(raw Docker env-file format; requires Compose 2.30+). Explicit Compose environment
values override that file. Keep it mode 0600 and `.state/` private.

Parallel installations must use independent data volumes. Import the entire data
volume only with the original writer stopped, then restart the original. Apply
any required offline migrations only to the new copy before starting it.
Credentials and data start identical; subsequent writes are independent.
Keep the old deployment and its data until a deliberate final cutover.

For an existing Cloudflare/Tailscale tunnel to `127.0.0.1:8080`, use HTTP mode
(`RAWROUTE_SITE=:80`, `RAWROUTE_HTTP_PORT=8080`) and set `RAWROUTE_PUBLIC_URL`
to the external HTTPS URL. Include both external origins in
`RAWROUTE_ALLOWED_ORIGINS` in the private runtime env file. Caddy trusts forwarded
headers from private-network proxies so external HTTPS sessions remain secure.

## Setup (when ready)

Point a public DNS hostname at the server and allow inbound TCP 80/443. Copy
`.env.example` to `.env` and set `RAWROUTE_HOST`. Caddy obtains and persists TLS
certificates automatically. This stack owns ports 80/443; do not run it beside
another proxy using those ports without adapting the deployment boundary.

Preview only (safe; no Docker commands or files written):

```sh
python3 deploy.py docker.io/rasyidrafi/rawroute:sha-<full-commit>
```

Deploy only after reviewing release/schema compatibility:

```sh
python3 deploy.py docker.io/rasyidrafi/rawroute:sha-<full-commit> --apply --schema-compatible
```

Use an actual published commit tag or image digest, not the literal placeholder.
The script resolves the pulled image to its local content ID before starting it.
Initial login uses password `123456` and requires a password change.

## Deployment behavior

1. Validate Compose and pull images while the old app remains available.
2. Persist an interrupted-deployment journal and enable proxy maintenance (503,
   Retry-After, no caching). No AI request retries or buffering are introduced.
3. Keep the proxy running; gracefully stop the only app writer (60-second grace).
4. Archive the entire stopped data volume into private `backups/`.
5. Start the new app; require database and CLIProxy readiness, not just liveness.
6. Reopen traffic only after readiness succeeds. On failure, automatically attempt
   to restart the previous image and reopen traffic after it becomes ready.

Downloads/builds are outside downtime. Downtime includes drain, full backup, and
startup; large data volumes increase it. Existing AI streams may finish during
drain, but are not transferable and can be interrupted at the shutdown deadline.
This is less downtime, NOT zero downtime. No parallel writers, rolling replicas,
automatic database conversion, or automatic data restoration are used. Recovery can
fail (for example, if Docker, storage, or the host fails), and abrupt process termination
can interrupt it; monitor the deployment until public service is confirmed.

The fixed external volume is `rawroute-production-data`. The script creates it
on first deploy. Existing installations must be deliberately moved/restored into
this volume with ALL writers stopped; this does not automatically adopt an old
Compose volume. Never run two deployments against the same data. The script
checks running Docker containers using this volume but cannot detect native
processes, remote writers, or bind-mount aliases. Keep one deployment owner.

This stack is AI-only with a managed engine. Executor/Storage are not enabled.
Do not use it to upgrade an installation requiring those features without
adapting its explicit deployment boundaries first.

## Recovery and migration releases

If automatic recovery fails, immediately inspect `.state/pending.json`, container logs,
and the backup, and restore service. If the deployment process is killed or the host
fails during maintenance, recovery must also be initiated manually.
For a data-compatible release, explicitly restart the previous locally available
image (does not restore data):

```sh
python3 deploy.py --recover
python3 deploy.py --recover --apply --schema-compatible
```

If the previous image was the cause of failure or is unavailable, repair manually;
do not repeatedly retry recovery blindly. First-install failures have no previous
image and require manual repair. Retain `.state/`, local previous images, and backups.
Do not prune images during an update/recovery or run `docker compose down -v`.

For incompatible data/schema/engine upgrades, DO NOT pass `--schema-compatible`.
Use the application release's stopped-writer migration procedure, full backup,
and explicit recovery plan. Old-image rollback may require restoring the entire
stopped volume and matching release together. This script deliberately does not
restore/overwrite data or guess migration commands. Backups contain credentials:
restrict access, encrypt off-host copies, test restoration, and manage retention.

The proxy is not recreated during normal updates. Updating Caddy/Caddyfile is a
separate maintenance operation; validate configuration and use graceful reload.
The final application readiness check is internal: separately verify public HTTPS,
DNS, certificate issuance, and firewall reachability. Do not use unattended cron
deployments or external Compose operations alongside this script's lock.

## Local checks (no Docker/deployment)

```sh
python3 -m unittest -v
```
