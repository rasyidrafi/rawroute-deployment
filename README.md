# RawRoute deployment

Linux, Docker Engine, Docker Compose v2 with `up --wait`, and Python 3 are required.
This is a standalone deployment repository. Nothing deploys on clone or test.

## Local source and parallel deployment

`python3 build.py` builds clean committed source from `../rawroute` for isolated
development only. Production deployment requires a published immutable image;
`deploy.py` does not deploy local builds.

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

This release is the stopped-writer Codex credential migration. Use the published
immutable image only after CI and the image are ready:

```sh
python3 deploy.py docker.io/rasyidrafi/rawroute:sha-<full-commit> --apply
```

Use an actual published commit tag or image digest, not the literal placeholder.
The script resolves the pulled image to its local content ID before starting it.
Initial login uses password `123456` and requires a password change.

## Deployment behavior

1. Pull and pin the candidate and previous images by local image ID; validate both
   Compose configurations and the offline migration helper before downtime.
2. Journal the previous image/config, enter Caddy maintenance (503,
   Retry-After, no caching), and verify the ingress gate.
3. Gracefully stop the only `/data` writer (60-second grace) and verify the
   volume is idle. Create and verify a private, full-volume backup containing
   SQLite/WAL, CLIProxy auth/config, and Storage state.
4. Run the candidate Codex importer preview, apply with `--writers-stopped`,
   and re-preview with network disabled and explicit legacy paths.
5. Start the candidate with CLIProxy settings removed and Storage host publishing
   disabled. Require database and internal Storage readiness, exact image/volume,
   and verify port 9000 is not reachable on the host.
6. Durably commit, then publish Storage only on `127.0.0.1:9000`, verify it, and
   reopen Caddy only after readiness. Before commit, failures restore the matching
   full-volume backup when data may have changed, then the prior image/config.
   After Storage is published, recovery never restores stale data; it resumes the
   committed candidate.

Downloads and image validation are outside downtime. Downtime includes drain,
full backup, offline migration, and candidate startup/readiness; large data
volumes increase it. Existing AI streams may finish during drain, but are not
transferable and can be interrupted at the shutdown deadline. This is less
downtime, NOT zero downtime. No parallel writers or rolling replicas are used.
Recovery can fail (for example, if Docker, storage, or the host fails), and abrupt
process termination can interrupt it; monitor the deployment until public service
is confirmed.

This migration requires an existing `rawroute-production` application and the
fixed external volume `rawroute-production-data`; it does not install a fresh
deployment or adopt another Compose volume. Never run two deployments against the
same data. The script checks running Docker containers using this volume but
cannot detect native processes, remote writers, or bind-mount aliases. Keep one
deployment owner.

Storage's S3 API listens over HTTP at `http://127.0.0.1:9000` on the host. It is
loopback-only and is not covered by Caddy's HTTPS listener. Remote S3 clients
need a separately configured TLS-terminating proxy that forwards to this local
endpoint; never expose port 9000 directly. Storage creates its encryption key in
the persistent data volume, which full-volume backups include. Preserve the
matching key when restoring; saved Storage credentials cannot be recovered
without it. Backups contain credentials; restrict access and encrypt off-host
copies.

## Recovery and migration releases

If automatic recovery fails, immediately inspect `.state/pending.json`, container logs,
and the backup, and restore service. If the deployment process is killed or the host
fails during maintenance, recovery must also be initiated manually.
If interrupted before the durable commit/Storage-publish boundary, recovery
restores the matching old data, image, and configuration. After that boundary it
resumes only the candidate and never restores the pre-migration snapshot:

```sh
python3 deploy.py --recover --apply
```

If the previous image was the cause of failure or is unavailable, repair manually;
do not repeatedly retry recovery blindly. First-install failures have no previous
image and require manual repair. Retain `.state/`, local previous images, and backups.
Do not prune images during an update/recovery or run `docker compose down -v`.

The script implements this Codex migration only; it does not infer compatibility,
retain CLIProxy at runtime, or accept a compatibility bypass. Backups contain
credentials: restrict access, encrypt off-host copies, test restoration, and
manage retention. No existing image, backup, or data is pruned or deleted.

The proxy is not recreated during normal updates. Updating Caddy/Caddyfile is a
separate maintenance operation; validate configuration and use graceful reload.
The final application readiness check is internal: separately verify public HTTPS,
DNS, certificate issuance, and firewall reachability. Do not use unattended cron
deployments or external Compose operations alongside this script's lock.

## Local checks (no Docker/deployment)

```sh
python3 -m unittest -v
```
