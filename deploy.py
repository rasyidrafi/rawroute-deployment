#!/usr/bin/env python3
"""Preview or run the stopped-writer Codex credential deployment transaction."""

import argparse
import errno
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
CONTROL = STATE / "control"
PENDING = STATE / "pending.json"
ACTIVE = STATE / "active.json"
COMPOSE = ROOT / "compose.yml"
ROLLBACK_COMPOSE = ROOT / "compose.rollback.yml"
RUNTIME_ENV = STATE / "runtime.env"
BACKUPS = ROOT / "backups"
VOLUME = "rawroute-production-data"
PROJECT = "rawroute-production"
APP_SERVICE = "rawroute"
PROXY_SERVICE = "proxy"
PROXY_IMAGE = "caddy:2.10.2-alpine"
HELPER_LABEL = "local.rawroute.deployment"
HEALTH_SCRIPT = """const r=await fetch('http://127.0.0.1:8080/api/health',{signal:AbortSignal.timeout(3000)});const j=await r.json();const s=j.dependencies?.storage;process.exit(r.ok&&j.status==='ok'&&j.service==='rawroute'&&j.dependencies?.database===true&&s?.enabled===true&&s?.listening===true?0:1)"""
OLD_HEALTH_SCRIPT = """const r=await fetch('http://127.0.0.1:8080/api/health',{signal:AbortSignal.timeout(3000)});const j=await r.json();const s=j.dependencies?.storage;process.exit(r.ok&&j.status==='ok'&&j.service==='rawroute'&&j.dependencies?.database===true&&j.dependencies?.cliproxy===true&&s?.enabled===true&&s?.listening===true?0:1)"""
SNAPSHOT_TIMEOUT = 6 * 60 * 60
IMPORT_TIMEOUT = 15 * 60


class DeploymentError(RuntimeError):
    """A deliberately secret-free deployment failure."""


DIAGNOSTIC_PHASES = frozenset({
    "startup", "unknown", "preflight-complete", "maintenance", "old-stopped",
    "snapshot-complete", "migration-preview", "migration-intent", "migration-complete",
    "candidate-boot-intent", "candidate-ready", "storage-opening", "storage-ready",
    "opening", "opened", "committed", "committed-recovery-needed", "rollback",
    "rollback-old-start", "rollback-storage-opening", "rollback-storage-ready",
    "rollback-opening", "rollback-opened", "rollback-committed", "rollback-failed",
    "rollback-committed-recovery-needed",
})
DIAGNOSTIC_OPERATIONS = frozenset({
    "start deployment", "prepare transaction artifacts", "preflight existing deployment",
    "persist initial journal", "enable and verify maintenance gate",
    "stop prior application and verify idle volume", "snapshot stopped volume",
    "verify stopped-volume snapshot", "preview Codex migration", "apply Codex migration",
    "snapshot post-migration volume", "validate post-migration state",
    "boot candidate without Storage", "commit and reopen ingress",
    "rollback to prior deployment", "recover committed candidate",
    "recover prior committed deployment", "resume interrupted deployment",
    "read recovery journal",
})
DIAGNOSTIC_LAST_OPERATIONS = DIAGNOSTIC_OPERATIONS | {"none", "read recovery journal"}


def safe_failure_message(error):
    """Return only fixed internal diagnostics; never forward arbitrary exception text."""
    if isinstance(error, OSError):
        return "A filesystem or operating-system operation failed."
    if isinstance(error, KeyboardInterrupt):
        return "Deployment was interrupted."
    if type(error) is DeploymentError:
        trace = error.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        internal = trace is not None and trace.tb_frame.f_globals.get("__name__") == __name__
        if not internal:
            return "An unexpected deployment exception occurred."
        message = str(error)
        if (len(message) <= 240 and not any(character in message for character in "\r\n/\\")
                and ".env" not in message.lower()
                and not re.search(r"(?:token|password|secret)\s*[:=]", message, re.IGNORECASE)):
            return message
    return "An unexpected deployment exception occurred."


def report_failure(error, *, phase, operation, last_completed, recovery):
    """Print a bounded diagnostic made only from trusted messages and enum values."""
    phase = phase if isinstance(phase, str) and phase in DIAGNOSTIC_PHASES else "unknown"
    operation = operation if isinstance(operation, str) and operation in DIAGNOSTIC_OPERATIONS else "start deployment"
    last_completed = (last_completed if isinstance(last_completed, str)
                      and last_completed in DIAGNOSTIC_LAST_OPERATIONS else "none")
    recovery = recovery if recovery in {"not-required", "recovered", "intervention-required"} else "intervention-required"
    print(
        f"Deployment failure: {safe_failure_message(error)}; phase={phase}; "
        f"last-completed={last_completed}; operation={operation}; recovery={recovery}.",
        file=sys.stderr,
    )
    try:
        error._rawroute_diagnostic_reported = True
    except Exception:
        pass


def immutable(image):
    return bool(
        re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image)
        or re.fullmatch(r"[\w./:-]+:sha-[0-9a-f]{40}", image)
    )


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_directory(path, mode, *, create=False):
    path = Path(path)
    if path.is_symlink():
        raise DeploymentError("Refusing a symlink in deployment state.")
    if create:
        if not path.exists():
            path.mkdir(mode=mode, parents=False)
            os.chmod(path, mode, follow_symlinks=False)
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        raise DeploymentError("Required deployment directory is unavailable.") from None
    permissions = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or (permissions & ~mode) or (permissions & 0o700) != 0o700
            or (mode == 0o755 and not permissions & 0o001)):
        raise DeploymentError("Deployment directory permissions are not private and supported.")
    return path


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise DeploymentError("Refusing a symlink in deployment state.")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def check_private_file(path, *, required=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise DeploymentError("A required private deployment file is missing.") from None
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise DeploymentError("A private deployment file has unsupported type or permissions.")
    return True


def regular_file(path, label):
    label = label if label in {"candidate Compose", "previous Compose"} else "deployment configuration"
    try:
        info = Path(path).lstat()
    except OSError:
        raise DeploymentError(f"The {label} file is missing.") from None
    if not stat.S_ISREG(info.st_mode):
        raise DeploymentError(f"The {label} file must be a regular file.")


def retired_engine_setting(key):
    return key in {"CLIPROXY", "CLI_PROXY", "MANAGEMENT_PASSWORD"} or key.startswith(("CLIPROXY_", "CLI_PROXY_"))


def sanitized_runtime_file(source, destination):
    """Copy non-engine runtime overrides privately without logging their values."""
    lines = []
    if source.exists() or source.is_symlink():
        check_private_file(source, required=True)
        try:
            with source.open("r", encoding="utf-8", newline="") as stream:
                for line in stream:
                    raw = line.rstrip("\r\n")
                    key, _separator, _value = raw.partition("=")
                    if retired_engine_setting(key.strip()):
                        continue
                    lines.append(line)
        except (OSError, UnicodeError):
            raise DeploymentError("The private runtime environment file cannot be safely prepared.") from None
    data = "".join(lines).encode()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise
    sync_directory(Path(destination).parent)


def make_image_ref(image_id, purpose):
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", image_id)
    if not match:
        raise DeploymentError("Docker returned an invalid immutable image ID.")
    return f"rawroute-production/{purpose}:sha256-{match.group(1)}"


class Docker:
    """Quiet Docker adapter: output is captured/discarded, never forwarded."""

    def run(self, args, *, capture=False, timeout=1800, stdout=None, env=None):
        try:
            result = subprocess.run(
                args,
                cwd=ROOT,
                env=env,
                check=False,
                text=False,
                stdout=subprocess.PIPE if capture else (stdout if stdout is not None else subprocess.DEVNULL),
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentError("Docker operation failed or timed out.") from None
        if result.returncode:
            raise DeploymentError("Docker operation failed.")
        return result.stdout.decode() if capture else None

    def compose(self, image, *args, file=COMPOSE, capture=False, timeout=1800):
        command = [
            "docker", "compose", "--project-name", PROJECT,
            "--project-directory", str(ROOT), "--env-file", str(ROOT / ".env"),
            "-f", str(file), *args,
        ]
        return self.run(command, capture=capture, timeout=timeout,
                        env={**os.environ, "RAWROUTE_IMAGE": image})

    def inspect(self, resource, *, image=False):
        command = ["docker", "image", "inspect", resource] if image else ["docker", "inspect", resource]
        try:
            value = json.loads(self.run(command, capture=True))
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
                raise ValueError
            return value[0]
        except (ValueError, json.JSONDecodeError):
            raise DeploymentError("Docker returned an invalid inspection result.") from None

    def image_id(self, image):
        return self.inspect(image, image=True).get("Id", "")

    def tag(self, source, destination):
        try:
            existing = self.image_id(destination)
        except DeploymentError:
            # Distinguish a missing local tag from a disconnected Docker daemon.
            self.run(["docker", "info", "--format", "{{.ID}}"])
            existing = None
        source_id = self.image_id(source)
        if existing:
            if existing != source_id:
                raise DeploymentError("A pinned local image name already refers to different content.")
            return
        self.run(["docker", "image", "tag", source, destination])
        if self.image_id(destination) != source_id:
            raise DeploymentError("Docker did not preserve the requested pinned image content.")

    def volume_users(self):
        output = self.run(["docker", "ps", "-q", "--no-trunc", "--filter", f"volume={VOLUME}"], capture=True)
        return output.split()

    def snapshot(self, image, volume, destination, transaction, helper):
        command = [
            "docker", "run", "--rm", "--name", helper, "--label", f"{HELPER_LABEL}={transaction}",
            "--network", "none", "--user", "0:0",
            "--mount", f"type=volume,src={volume},dst=/source,readonly,volume-nocopy",
            "--entrypoint", "/bin/sh", image, "-ec",
            "tar --numeric-owner --xattrs --xattrs-include='*' --acls -cpf - -C /source .",
        ]
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as archive:
            try:
                result = subprocess.run(command, cwd=ROOT, stdout=archive, stderr=subprocess.PIPE,
                                        timeout=SNAPSHOT_TIMEOUT, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise DeploymentError("The stopped-volume backup operation failed or timed out.") from None
            archive.flush()
            os.fsync(archive.fileno())
        sync_directory(Path(destination).parent)
        if result.returncode:
            raise DeploymentError("The stopped-volume backup operation failed.")

    def restore(self, image, volume, backup, transaction, helper):
        command = [
            "docker", "run", "--rm", "--name", helper, "--label", f"{HELPER_LABEL}={transaction}",
            "--network", "none", "--user", "0:0",
            "--mount", f"type=volume,src={volume},dst=/target,volume-nocopy",
            "--mount", f"type=bind,src={Path(backup).parent},dst=/backup,readonly",
            "--entrypoint", "/bin/sh", image, "-ec",
            "find -P /target -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
            "tar --numeric-owner --same-owner --xattrs --xattrs-include='*' --acls -xpf /backup/volume.tar -C /target; "
            "tar --numeric-owner --same-owner --xattrs --xattrs-include='*' --acls -dpf /backup/volume.tar -C /target; "
            "sync -f /target",
        ]
        self.run(command, timeout=SNAPSHOT_TIMEOUT)

    def importer_preview(self, image, snapshot, backup_root, transaction, helper):
        """Run the read-only planner against a writable disposable clone of a verified snapshot."""
        command = [
            "docker", "run", "--rm", "--name", helper, "--label", f"{HELPER_LABEL}={transaction}",
            "--network", "none", "--user", "0:0",
            # No source makes Docker allocate an anonymous, container-scoped
            # volume. --rm and stop_helpers' `docker rm -v` discard its SQLite
            # sidecars even when the helper is interrupted.
            "--mount", "type=volume,dst=/data,volume-nocopy",
            "--mount", f"type=bind,src={Path(snapshot)},dst=/snapshot.tar,readonly",
            "--mount", f"type=bind,src={backup_root},dst=/backup,readonly",
            "--entrypoint", "/bin/sh", image, "-ec",
            "tar --numeric-owner --same-owner --xattrs --xattrs-include='*' --acls -xpf /snapshot.tar -C /data; "
            "tar --numeric-owner --same-owner --xattrs --xattrs-include='*' --acls -dpf /snapshot.tar -C /data; "
            "sync -f /data; "
            "exec bun run --no-env-file migrations/codex-credentials.js "
            "--sqlite /data/rawroute.sqlite --auth-dir /data/cliproxy/auth --config /data/cliproxy/config.yaml",
        ]
        output = self.run(command, capture=True, timeout=IMPORT_TIMEOUT)
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, json.JSONDecodeError):
            raise DeploymentError("The Codex importer preview returned an invalid result.") from None

    def importer(self, image, volume, backup_root, transaction, helper, *, apply):
        """Apply only; previews must use importer_preview's disposable copy."""
        if not apply:
            raise DeploymentError("Read-only Codex previews require a verified disposable snapshot copy.")
        volume_mount = f"type=volume,src={volume},dst=/data,volume-nocopy"
        backup_mount = f"type=bind,src={backup_root},dst=/backup"
        command = [
            "docker", "run", "--rm", "--name", helper, "--label", f"{HELPER_LABEL}={transaction}",
            "--network", "none", "--user", "0:0",
            "--mount", volume_mount,
            "--mount", backup_mount,
            "--entrypoint", "bun", image, "run", "--no-env-file",
            "migrations/codex-credentials.js",
            "--sqlite", "/data/rawroute.sqlite",
            "--auth-dir", "/data/cliproxy/auth",
            "--config", "/data/cliproxy/config.yaml",
        ]
        command.extend(["--apply", "--writers-stopped", "--backup-dir", "/backup/importer"])
        output = self.run(command, capture=True, timeout=IMPORT_TIMEOUT)
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, json.JSONDecodeError):
            raise DeploymentError("The Codex importer returned an invalid result.") from None

    def helper_preflight(self, image, backup_root):
        command = [
            "docker", "run", "--rm", "--network", "none", "--user", "0:0",
            "--mount", f"type=bind,src={backup_root},dst=/backup",
            "--entrypoint", "/bin/sh", image, "-ec",
            "test -f /app/dist/migrations/codex-credentials.js; "
            "command -v tar >/dev/null; "
            "tar --help | grep -q -- --xattrs; "
            "tar --help | grep -q -- --acls; "
            "tar --help | grep -q -- --compare; "
            "find -P / -maxdepth 0 -type d >/dev/null; "
            "sync --help | grep -q -- --file-system; "
            "probe=$(mktemp /backup/.preflight.XXXXXX); rm -f \"$probe\"",
        ]
        self.run(command, timeout=60)

    def stop_helpers(self, transaction):
        filter_args = ["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={HELPER_LABEL}={transaction}"]
        try:
            identifiers = self.run(filter_args, capture=True).split()
        except DeploymentError:
            raise DeploymentError("A deployment helper may still be using the data volume.") from None
        for identifier in identifiers:
            item = self.inspect(identifier)
            labels = (item.get("Config", {}).get("Labels") or {})
            if labels.get(HELPER_LABEL) != transaction:
                raise DeploymentError("A deployment helper has an unexpected owner.")
            if item.get("State", {}).get("Running"):
                self.run(["docker", "stop", "--time", "30", identifier], timeout=45)
            remaining = self.run(filter_args, capture=True).split()
            if identifier not in remaining:
                continue
            current = self.inspect(identifier)
            if current.get("State", {}).get("Running"):
                raise DeploymentError("A deployment helper could not be stopped.")
            # `-v` removes only anonymous helper mounts; named production volumes
            # such as /data are retained.
            self.run(["docker", "rm", "-v", identifier])


def image_reference(image_id, purpose):
    return make_image_ref(image_id, purpose)


def compose_container_ids(docker, image, service, file=COMPOSE, all_containers=True):
    args = ["ps", "-aq" if all_containers else "-q", service]
    output = docker.compose(image, *args, file=file, capture=True)
    return output.split()


def compose_service_hash(docker, image, file, service):
    output = docker.compose(image, "config", "--hash", service, file=file, capture=True)
    values = [token for line in output.splitlines() if (token := line.strip().split())]
    if not values:
        raise DeploymentError("Compose did not return a service configuration fingerprint.")
    digest = values[-1][-1]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DeploymentError("Compose returned an invalid service configuration fingerprint.")
    return digest


def wait_healthy(docker, identifier, image_id, timeout=240):
    deadline = time.monotonic() + timeout
    while True:
        container = docker.inspect(identifier)
        check_service_container(container, image_id=image_id, running=True)
        health = container.get("State", {}).get("Health", {}).get("Status")
        if health == "healthy":
            return container
        if health == "unhealthy" or time.monotonic() >= deadline:
            raise DeploymentError("The application did not become healthy before the recovery deadline.")
        time.sleep(2)


def check_volume_mount(container):
    mounts = container.get("Mounts") or []
    if len(mounts) != 1:
        raise DeploymentError("The application does not use the single expected production data volume.")
    mount = mounts[0]
    if (mount.get("Type") != "volume" or mount.get("Name") != VOLUME
            or mount.get("Destination") != "/data" or not mount.get("RW")):
        raise DeploymentError("The application data mount is not the expected writable production volume.")


def check_storage_binding(container):
    bindings = (container.get("HostConfig", {}).get("PortBindings") or {}).get("9000/tcp") or []
    if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1" or bindings[0].get("HostPort") != "9000":
        raise DeploymentError("Storage must remain bound only to host loopback on port 9000.")
    if (container.get("NetworkSettings", {}).get("Ports") or {}).get("9000/tcp") != bindings:
        raise DeploymentError("Docker's active Storage binding does not match the loopback-only configuration.")


def check_service_container(container, *, image_id=None, running=None):
    labels = (container.get("Config", {}).get("Labels") or {})
    if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != APP_SERVICE:
        raise DeploymentError("The application container is not the expected Compose service.")
    if container.get("Name") != f"/{PROJECT}-{APP_SERVICE}-1":
        raise DeploymentError("The application container name is not the expected production instance.")
    check_volume_mount(container)
    if image_id is not None and container.get("Image") != image_id:
        raise DeploymentError("The running application image does not match the pinned image.")
    if running is not None and bool(container.get("State", {}).get("Running")) != running:
        raise DeploymentError("The application container is in an unexpected running state.")


def check_candidate_engine_settings(container):
    for item in (container.get("Config", {}).get("Env") or []):
        key, separator, _value = item.partition("=")
        if separator and retired_engine_setting(key):
            raise DeploymentError("The candidate contains retired engine settings.")


def site_host(value):
    if not isinstance(value, str) or not value or any(character.isspace() for character in value) or "," in value:
        raise DeploymentError("Caddy must use one explicit HTTPS hostname for the maintenance probe.")
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise DeploymentError("Caddy's site address is not supported for a verified maintenance probe.")
    try:
        port = parsed.port
    except ValueError:
        raise DeploymentError("Caddy's site address has an invalid port.") from None
    if port not in (None, 443):
        raise DeploymentError("Caddy's site address does not provide the expected HTTPS endpoint.")
    host = parsed.hostname
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith(".") or host.endswith(".") or ".." in host:
        raise DeploymentError("Caddy's site hostname is not supported for the maintenance probe.")
    return host


def site_target(value):
    if value == ":80":
        return "http", "127.0.0.1"
    return "https", site_host(value)


class Ingress:
    def __init__(self, docker, proxy):
        self.docker = docker
        self.proxy = proxy
        config = proxy.get("Config") or {}
        labels = config.get("Labels") or {}
        if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != PROXY_SERVICE:
            raise DeploymentError("The ingress container is not the expected Compose proxy.")
        if not proxy.get("State", {}).get("Running"):
            raise DeploymentError("The ingress proxy is not running.")
        site = None
        for item in config.get("Env") or []:
            key, separator, value = item.partition("=")
            if separator and key == "RAWROUTE_SITE":
                site = value
                break
        self.scheme, self.host = site_target(site)
        mounts = proxy.get("Mounts") or []
        control_mount = [mount for mount in mounts if mount.get("Destination") == "/srv/control"]
        if (len(control_mount) != 1 or control_mount[0].get("RW")
                or Path(control_mount[0].get("Source", "")).resolve() != CONTROL.resolve()):
            raise DeploymentError("Caddy does not have the expected read-only maintenance-control mount.")
        port_key = "80/tcp" if self.scheme == "http" else "443/tcp"
        bindings = (proxy.get("HostConfig", {}).get("PortBindings") or {}).get(port_key) or []
        if len(bindings) != 1 or bindings[0].get("HostPort", "") == "":
            raise DeploymentError("Caddy does not publish the expected ingress port for the local verification probe.")
        self.port = int(bindings[0]["HostPort"])
        if self.port < 1 or self.port > 65535:
            raise DeploymentError("Caddy's HTTPS ingress port is invalid.")

    def request(self, path="/api/health"):
        if self.scheme == "http":
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        else:
            class LocalHTTPSConnection(http.client.HTTPSConnection):
                def connect(inner_self):
                    raw = socket.create_connection(("127.0.0.1", inner_self.port), timeout=inner_self.timeout)
                    inner_self.sock = inner_self._context.wrap_socket(raw, server_hostname=self.host)

            connection = LocalHTTPSConnection(
                self.host, self.port, timeout=5,
                context=ssl._create_unverified_context(),
            )
        try:
            connection.request("GET", path, headers={"Host": self.host, "Connection": "close"})
            response = connection.getresponse()
            body = response.read(65536)
            return response.status, {key.lower(): value for key, value in response.getheaders()}, body
        except (OSError, http.client.HTTPException, socket.timeout):
            raise DeploymentError("The ingress probe failed.") from None
        finally:
            connection.close()

    def assert_gated(self):
        status, headers, body = self.request()
        if (status != 503 or headers.get("retry-after") != "30"
                or headers.get("cache-control") != "no-store"
                or body.rstrip(b"\n") != b"RawRoute is being updated. Please try again shortly."):
            raise DeploymentError("Caddy did not verify the expected maintenance response.")

    def assert_healthy(self, *, require_storage=True):
        status, _headers, body = self.request()
        try:
            result = json.loads(body)
            dependencies = result.get("dependencies") or {}
            storage = dependencies.get("storage") or {}
        except (ValueError, AttributeError, json.JSONDecodeError):
            raise DeploymentError("The public health endpoint returned an invalid response.") from None
        if (status != 200 or result.get("status") != "ok" or result.get("service") != "rawroute"
                or dependencies.get("database") is not True
                or (require_storage and (storage.get("enabled") is not True or storage.get("listening") is not True))):
            raise DeploymentError("The public health endpoint is not ready.")


def verify_snapshot(archive):
    archive = Path(archive)
    check_private_file(archive, required=True)
    names = {}

    def normalized_path(raw):
        if not isinstance(raw, str) or "\x00" in raw:
            raise DeploymentError("The stopped-volume backup contains an unsafe path.")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            raise DeploymentError("The stopped-volume backup contains an unsafe path.")
        parts = tuple(part for part in path.parts if part not in ("", "."))
        return "/".join(parts) if parts else "."

    try:
        with tarfile.open(archive, mode="r:") as snapshot:
            for member in snapshot.getmembers():
                name = normalized_path(member.name)
                if name in names:
                    raise DeploymentError("The stopped-volume backup contains a duplicate path.")
                if name == "." and not member.isdir():
                    raise DeploymentError("The stopped-volume backup root must be a directory.")
                if not (member.isdir() or member.isfile() or member.issym()):
                    raise DeploymentError("The stopped-volume backup contains an unsupported file type.")
                names[name] = member
                if member.isfile():
                    stream = snapshot.extractfile(member)
                    if stream is None:
                        raise DeploymentError("The stopped-volume backup is incomplete.")
                    read_bytes = 0
                    while chunk := stream.read(1024 * 1024):
                        read_bytes += len(chunk)
                    stream.close()
                    if read_bytes != member.size:
                        raise DeploymentError("The stopped-volume backup is truncated.")
    except (OSError, tarfile.TarError, EOFError):
        raise DeploymentError("The stopped-volume backup failed archive verification.") from None

    # Validate the complete archive map after parsing so entries cannot hide
    # beneath a symlink merely by appearing earlier in tar order.
    for name, member in names.items():
        if name == ".":
            continue
        parts = tuple(name.split("/"))
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            parent = names.get(ancestor)
            if parent is None or not parent.isdir():
                raise DeploymentError("The stopped-volume backup has a missing or non-directory ancestor.")
        if not member.issym():
            continue
        if name == "cliproxy/auth" or name.startswith("cliproxy/auth/"):
            raise DeploymentError("The legacy Codex auth backup contains an unsupported symbolic link.")
        target = member.linkname
        if not isinstance(target, str) or not target or "\x00" in target:
            raise DeploymentError("The stopped-volume backup contains an unsafe symbolic link.")
        target_path = PurePosixPath(target)
        if target_path.is_absolute() or ".." in target_path.parts:
            raise DeploymentError("The stopped-volume backup contains an unsafe symbolic link.")
        target_parts = tuple(part for part in target_path.parts if part not in ("", "."))
        parent_parts = tuple(parts[:-1])
        resolved_parts = parent_parts + target_parts
        if (len(resolved_parts) <= len(parts)
                and parts[:len(resolved_parts)] == resolved_parts):
            raise DeploymentError("The stopped-volume backup contains a cyclic symbolic link.")
        resolved = "/".join(resolved_parts) if resolved_parts else "."
        destination = names.get(resolved)
        if destination is None or not destination.isdir():
            raise DeploymentError("The stopped-volume backup contains a dangling or chained symbolic link.")
        for index in range(1, len(resolved_parts)):
            ancestor = "/".join(resolved_parts[:index])
            parent = names.get(ancestor)
            if parent is None or not parent.isdir():
                raise DeploymentError("The stopped-volume backup symbolic link traverses a non-directory.")

    database = names.get("rawroute.sqlite")
    auth = names.get("cliproxy/auth")
    config = names.get("cliproxy/config.yaml")
    if (database is None or not database.isfile() or auth is None or not auth.isdir()
            or config is None or not config.isfile()):
        raise DeploymentError("The stopped-volume backup is missing required database or legacy Codex inputs.")
    for name, member in names.items():
        if (name == "cliproxy/auth" or name.startswith("cliproxy/auth/")) and not (member.isdir() or member.isfile()):
            raise DeploymentError("The legacy Codex auth backup contains unsupported entries.")
    sync_file(archive)
    return {
        "bytes": archive.stat().st_size,
        "files": sum(member.isfile() for member in names.values()),
        "sha256": private_file_digest(archive),
    }


def sync_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_file_digest(path):
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise DeploymentError("A deployment configuration file is not a regular file.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        raise DeploymentError("A deployment configuration file cannot be verified.") from None


def check_archive_exists(archive):
    try:
        info = Path(archive).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not (stat.S_IMODE(info.st_mode) & 0o077)


def make_candidate_compose(transaction_dir, transaction):
    regular_file(COMPOSE, "candidate Compose")
    regular_file(ROLLBACK_COMPOSE, "previous Compose")
    source = COMPOSE.read_text(encoding="utf-8")
    old = "path: ./.state/runtime.env"
    new = f"path: ./.state/transactions/{transaction}/runtime.env"
    if source.count(old) != 1:
        raise DeploymentError("The candidate Compose file does not have the expected runtime environment boundary.")
    final_source = source.replace(old, new, 1)
    port_mapping = '    ports:\n      - "127.0.0.1:9000:9000"\n'
    if final_source.count(port_mapping) != 1:
        raise DeploymentError("The candidate Compose file does not have the expected loopback Storage port.")
    stage_source = final_source.replace(port_mapping, "", 1)

    def write_compose(path, contents):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())

    final = transaction_dir / "compose.final.yml"
    stage = transaction_dir / "compose.stage.yml"
    rollback_source = ROLLBACK_COMPOSE.read_text(encoding="utf-8")
    if rollback_source.count(port_mapping) != 1:
        raise DeploymentError("The previous Compose file does not have the expected loopback Storage port.")
    rollback_stage = transaction_dir / "compose.rollback.stage.yml"
    write_compose(final, final_source)
    write_compose(stage, stage_source)
    write_compose(rollback_stage, rollback_source.replace(port_mapping, "", 1))
    sanitized_runtime_file(RUNTIME_ENV, transaction_dir / "runtime.env")
    sync_directory(transaction_dir)
    return stage, final, rollback_stage


def storage_port_open():
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(2)
    try:
        connection.connect(("127.0.0.1", 9000))
        return True
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return False
        raise DeploymentError("The loopback Storage port state cannot be verified.") from None
    finally:
        connection.close()


def check_storage_unpublished(container):
    configured = (container.get("HostConfig", {}).get("PortBindings") or {}).get("9000/tcp") or []
    active = (container.get("NetworkSettings", {}).get("Ports") or {}).get("9000/tcp") or []
    if configured or any(binding and binding.get("HostPort") for binding in active):
        raise DeploymentError("Storage must not be published to host clients before the commit boundary.")


def check_private_compose_network(docker, app, proxy):
    network_name = f"{PROJECT}_default"
    for container in (app, proxy):
        networks = container.get("NetworkSettings", {}).get("Networks") or {}
        if set(networks) != {network_name}:
            raise DeploymentError("The application and ingress must use only the deployment's private network.")
    network = docker.inspect(network_name)
    connected = set((network.get("Containers") or {}).keys())
    expected = {app.get("Id"), proxy.get("Id")}
    if None in expected or connected != expected:
        raise DeploymentError("Unexpected containers share the Storage network; refusing pre-commit access.")


def maintenance_present():
    path = CONTROL / "maintenance"
    if path.is_symlink():
        raise DeploymentError("The maintenance gate is a symlink; refusing to alter ingress state.")
    return path.exists()


def enable_maintenance(transaction):
    path = CONTROL / "maintenance"
    if path.is_symlink():
        raise DeploymentError("The maintenance gate is a symlink; refusing to alter ingress state.")
    if path.exists():
        verify_maintenance_marker(transaction)
        sync_directory(CONTROL)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".maintenance-{transaction}-", dir=CONTROL)
    os.fchmod(descriptor, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(transaction)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            verify_maintenance_marker(transaction)
        sync_directory(CONTROL)
    except OSError:
        raise DeploymentError("The maintenance gate could not be durably published.") from None
    finally:
        try:
            os.unlink(temporary)
            sync_directory(CONTROL)
        except FileNotFoundError:
            pass
        except OSError:
            # The published marker is a complete, separately fsynced file. An
            # abandoned temporary name is harmless and contains no secret.
            pass


def verify_maintenance_marker(transaction):
    path = CONTROL / "maintenance"
    if path.is_symlink():
        raise DeploymentError("The maintenance gate is a symlink; refusing to alter ingress state.")
    try:
        info = path.lstat()
        value = path.read_text(encoding="ascii")
    except OSError:
        raise DeploymentError("The maintenance gate cannot be verified.") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or value != transaction):
        raise DeploymentError("A maintenance gate exists outside this deployment transaction.")


def disable_maintenance(transaction):
    path = CONTROL / "maintenance"
    if not path.exists():
        return
    verify_maintenance_marker(transaction)
    path.unlink()
    sync_directory(CONTROL)


def clear_pending():
    if PENDING.is_symlink():
        raise DeploymentError("The deployment journal is a symlink.")
    if PENDING.exists():
        PENDING.unlink()
        sync_directory(STATE)


def helper_name(transaction, action):
    return f"rr-{transaction[:16]}-{action}"


class Transaction:
    def __init__(self, docker, record, candidate_stage_compose, candidate_compose,
                 rollback_stage_compose, ingress):
        self.docker = docker
        self.record = record
        self.candidate_stage_compose = candidate_stage_compose
        self.candidate_compose = candidate_compose
        self.rollback_stage_compose = rollback_stage_compose
        self.ingress = ingress
        self.traffic_may_have_opened = False
        self.completed = False

    def save(self, **changes):
        updated = {**self.record, **changes}
        save_json(PENDING, updated)
        self.record.clear()
        self.record.update(updated)

    def complete(self, image, image_id, **details):
        if maintenance_present():
            raise DeploymentError("Cannot complete a deployment while maintenance is enabled.")
        save_json(ACTIVE, {
            "status": "complete",
            "transaction": self.record["transaction"],
            "image": image,
            "imageId": image_id,
            "backup": self.record["backup"],
            **details,
        })
        # Once ACTIVE is durable and the caller has verified health with the
        # gate open, cleanup must not trigger traffic-changing recovery.
        self.completed = True
        cleanup_completed_transaction(self.record)

    def assert_no_writers(self):
        if self.docker.volume_users():
            raise DeploymentError("A container still uses the production data volume; refusing backup or restoration.")

    def verify_compose_artifacts(self):
        if self.record.get("previous_compose") != str(ROLLBACK_COMPOSE):
            raise DeploymentError("The recorded previous Compose configuration is not available.")
        if private_file_digest(ROLLBACK_COMPOSE) != self.record.get("previous_compose_sha256"):
            raise DeploymentError("The previous Compose configuration changed since this transaction began.")
        check_private_file(self.candidate_compose, required=True)
        if private_file_digest(self.candidate_compose) != self.record.get("candidate_compose_sha256"):
            raise DeploymentError("The candidate Compose configuration changed since this transaction began.")
        check_private_file(self.candidate_stage_compose, required=True)
        if private_file_digest(self.candidate_stage_compose) != self.record.get("candidate_stage_compose_sha256"):
            raise DeploymentError("The staged candidate Compose configuration changed since this transaction began.")
        check_private_file(self.rollback_stage_compose, required=True)
        if private_file_digest(self.rollback_stage_compose) != self.record.get("rollback_stage_compose_sha256"):
            raise DeploymentError("The staged previous Compose configuration changed since this transaction began.")
        runtime_env = self.candidate_compose.parent / "runtime.env"
        check_private_file(runtime_env, required=True)
        if private_file_digest(runtime_env) != self.record.get("candidate_runtime_sha256"):
            raise DeploymentError("The private candidate runtime environment changed since this transaction began.")

    def verify_image_refs(self):
        if (self.docker.image_id(self.record["target_ref"]) != self.record["target_image_id"]
                or self.docker.image_id(self.record["old_ref"]) != self.record["old_image_id"]):
            raise DeploymentError("A pinned local image reference changed; refusing recovery or cutover.")

    def verify_previous_compose_hashes(self):
        image = self.record["old_ref"]
        expected = self.record["rollback_compose_hashes"]
        actual = {
            "application": compose_service_hash(self.docker, image, ROLLBACK_COMPOSE, APP_SERVICE),
            "stage": compose_service_hash(self.docker, image, self.rollback_stage_compose, APP_SERVICE),
        }
        actual["proxy"] = compose_service_hash(self.docker, image, ROLLBACK_COMPOSE, PROXY_SERVICE)
        if actual != expected:
            raise DeploymentError("The effective previous Compose configuration changed; refusing rollback recreation.")

    def assert_single_transaction_writer(self, image_ids):
        users = self.docker.volume_users()
        for identifier in users:
            container = self.docker.inspect(identifier)
            labels = (container.get("Config", {}).get("Labels") or {})
            if (labels.get("com.docker.compose.project") != PROJECT
                    or labels.get("com.docker.compose.service") != APP_SERVICE
                    or container.get("Image") not in image_ids):
                raise DeploymentError("An unexpected container uses the production data volume.")
            self.docker.run(["docker", "stop", "--time", "60", identifier], timeout=75)
            if self.docker.inspect(identifier).get("State", {}).get("Running"):
                raise DeploymentError("A production volume writer could not be stopped.")
        self.assert_no_writers()

    def stop_helpers(self):
        self.docker.stop_helpers(self.record["transaction"])

    def start_previous(self):
        self.verify_compose_artifacts()
        self.verify_image_refs()
        self.verify_previous_compose_hashes()
        self.stop_helpers()
        self.assert_no_writers()
        self.docker.compose(self.record["old_ref"], "up", "-d", "--no-deps", "--pull", "never",
                            "--wait", "--wait-timeout", "240", APP_SERVICE,
                            file=self.rollback_stage_compose, timeout=300)
        identifiers = compose_container_ids(self.docker, self.record["old_ref"], APP_SERVICE, self.rollback_stage_compose)
        if len(identifiers) != 1:
            raise DeploymentError("The previous application could not be restored as one container.")
        previous = self.docker.inspect(identifiers[0])
        check_service_container(previous, image_id=self.record["old_image_id"], running=True)
        previous_labels = (previous.get("Config", {}).get("Labels") or {})
        if previous_labels.get("com.docker.compose.config-hash") != self.record["rollback_compose_hashes"]["stage"]:
            raise DeploymentError("The staged previous application does not match its effective Compose fingerprint.")
        check_storage_unpublished(previous)
        if storage_port_open():
            raise DeploymentError("The previous Storage API became reachable before its recovery commit.")
        if previous.get("State", {}).get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("The previous application did not become healthy after restoration.")
        self.docker.run(["docker", "exec", identifiers[0], "bun", "-e", OLD_HEALTH_SCRIPT], timeout=10)
        self.record["old_container"] = identifiers[0]

    def start_previous_committed(self):
        self.verify_compose_artifacts()
        self.verify_image_refs()
        self.verify_previous_compose_hashes()
        self.stop_helpers()
        users = self.docker.volume_users()
        if len(users) > 1:
            raise DeploymentError("An unexpected container uses the production data volume during recovery.")
        for identifier in users:
            previous_stage = self.docker.inspect(identifier)
            labels = (previous_stage.get("Config", {}).get("Labels") or {})
            if (labels.get("com.docker.compose.project") != PROJECT
                    or labels.get("com.docker.compose.service") != APP_SERVICE
                    or previous_stage.get("Image") != self.record["old_image_id"]):
                raise DeploymentError("An unexpected container uses the production data volume during recovery.")
        self.docker.compose(self.record["old_ref"], "up", "-d", "--no-deps", "--pull", "never",
                            "--wait", "--wait-timeout", "240", APP_SERVICE,
                            file=ROLLBACK_COMPOSE, timeout=300)
        identifiers = compose_container_ids(self.docker, self.record["old_ref"], APP_SERVICE, ROLLBACK_COMPOSE)
        if len(identifiers) != 1:
            raise DeploymentError("The previous application could not be restored as one container.")
        previous = self.docker.inspect(identifiers[0])
        check_service_container(previous, image_id=self.record["old_image_id"], running=True)
        previous_labels = (previous.get("Config", {}).get("Labels") or {})
        if previous_labels.get("com.docker.compose.config-hash") != self.record["rollback_compose_hashes"]["application"]:
            raise DeploymentError("The previous application does not match its effective Compose fingerprint.")
        check_storage_binding(previous)
        if previous.get("State", {}).get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("The previous application did not become healthy after restoration.")
        self.docker.run(["docker", "exec", identifiers[0], "bun", "-e", OLD_HEALTH_SCRIPT], timeout=10)
        if not storage_port_open():
            raise DeploymentError("The previous Storage loopback listener is not reachable.")
        self.record["old_container"] = identifiers[0]

    def finish_previous_open(self):
        transaction = self.record["transaction"]
        self.save(phase="rollback-storage-opening", recovery="old", traffic_may_have_opened=True)
        self.traffic_may_have_opened = True
        self.start_previous_committed()
        if self.ingress:
            self.ingress.assert_gated()
        self.save(phase="rollback-storage-ready", recovery="old", traffic_may_have_opened=True)
        self.save(phase="rollback-opening", recovery="old", traffic_may_have_opened=True)
        disable_maintenance(transaction)
        self.traffic_may_have_opened = True
        self.save(phase="rollback-opened", recovery="old", traffic_may_have_opened=True)
        if self.ingress:
            self.ingress.assert_healthy(require_storage=True)
        self.complete(self.record["old_ref"], self.record["old_image_id"], recovered=True)

    def restore_volume_if_needed(self):
        if not self.record.get("data_may_change"):
            return
        if not self.record.get("snapshot_complete") or not check_archive_exists(self.record["backup"]):
            raise DeploymentError("The complete pre-migration backup is unavailable; data restoration cannot proceed.")
        snapshot = verify_snapshot(self.record["backup"])
        if snapshot.get("sha256") != (self.record.get("snapshot") or {}).get("sha256"):
            raise DeploymentError("The stopped-volume backup no longer matches its durable journal digest.")
        self.verify_image_refs()
        self.stop_helpers()
        self.assert_single_transaction_writer({self.record["old_image_id"], self.record["target_image_id"]})
        self.assert_no_writers()
        self.docker.restore(
            self.record["target_image_id"], VOLUME, self.record["backup"],
            self.record["transaction"], helper_name(self.record["transaction"], "restore"),
        )
        self.assert_no_writers()

    def rollback_preopen(self, *, gate_already_active=False):
        transaction = self.record["transaction"]
        try:
            if not gate_already_active:
                enable_maintenance(transaction)
            self.save(phase="rollback", recovery="pre-open")
            if self.ingress:
                try:
                    self.ingress.assert_gated()
                except Exception:
                    # Continue restoring the matching old data/image. It is safer
                    # to restore service than to leave an untouched old writer stopped.
                    if self.record.get("data_may_change"):
                        raise DeploymentError("Ingress gating cannot be verified; refusing to restore a snapshot while requests may be active.") from None
            if self.record.get("data_may_change") and not self.record.get("gate_verified"):
                raise DeploymentError("The journal does not prove that ingress was gated before data could change.")
            self.stop_helpers()
            self.assert_single_transaction_writer({self.record["old_image_id"], self.record["target_image_id"]})
            self.restore_volume_if_needed()
            # The restored old volume becomes authoritative before booting it.
            # Its background startup writes must never be replaced by the snapshot.
            self.save(phase="rollback-old-start", recovery="old", traffic_may_have_opened=True)
            self.traffic_may_have_opened = True
            self.start_previous()
            if self.ingress:
                self.ingress.assert_gated()
            self.finish_previous_open()
        except Exception:
            if self.record.get("phase") == "rollback-opening" and not maintenance_present():
                self.traffic_may_have_opened = True
            if self.traffic_may_have_opened:
                try:
                    enable_maintenance(transaction)
                    self.save(phase="rollback-committed", recovery="old", traffic_may_have_opened=True)
                    self.recover_previous_committed()
                    return
                except Exception:
                    try:
                        enable_maintenance(transaction)
                        self.save(phase="rollback-committed-recovery-needed", recovery="old", traffic_may_have_opened=True)
                    except Exception:
                        pass
                    raise DeploymentError("Previous application recovery failed after reopening; maintenance remains enabled. Run recovery with --recover --apply.") from None
            # Keep a durable recovery record and do not reopen an unverified service.
            try:
                enable_maintenance(transaction)
                self.save(phase="rollback-failed", recovery="pre-open")
            except Exception:
                pass
            raise DeploymentError("Automatic recovery failed; maintenance remains enabled. Run recovery with --recover --apply.") from None

    def recover_previous_committed(self):
        """Resume the restored old deployment without ever restoring stale data again."""
        transaction = self.record["transaction"]
        self.traffic_may_have_opened = True
        try:
            enable_maintenance(transaction)
            if self.ingress:
                self.ingress.assert_gated()
            # A visible phase may have been renamed into place without its parent
            # directory fsync completing. Reaffirm the commit durably before any
            # restart can publish loopback Storage.
            self.save(phase="rollback-committed", recovery="old", traffic_may_have_opened=True)
            self.stop_helpers()
            self.assert_single_transaction_writer({self.record["old_image_id"], self.record["target_image_id"]})
            self.finish_previous_open()
        except Exception:
            try:
                enable_maintenance(transaction)
                self.save(phase="rollback-committed-recovery-needed", recovery="old", traffic_may_have_opened=True)
            except Exception:
                pass
            raise DeploymentError("Previous application recovery failed; snapshot rollback is prohibited. Maintenance remains enabled.") from None

    def candidate_ready(self):
        self.verify_compose_artifacts()
        self.verify_image_refs()
        self.assert_no_writers()
        self.docker.compose(self.record["target_ref"], "up", "-d", "--no-deps", "--pull", "never",
                            "--wait", "--wait-timeout", "240", APP_SERVICE,
                            file=self.candidate_stage_compose, timeout=300)
        identifiers = compose_container_ids(self.docker, self.record["target_ref"], APP_SERVICE, self.candidate_stage_compose)
        if len(identifiers) != 1:
            raise DeploymentError("The candidate application did not start as one container.")
        candidate = self.docker.inspect(identifiers[0])
        check_service_container(candidate, image_id=self.record["target_image_id"], running=True)
        check_candidate_engine_settings(candidate)
        check_storage_unpublished(candidate)
        if storage_port_open():
            raise DeploymentError("The loopback Storage API is reachable before the durable commit boundary.")
        if candidate.get("State", {}).get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("The candidate application did not become healthy.")
        self.docker.run(["docker", "exec", identifiers[0], "bun", "-e", HEALTH_SCRIPT], timeout=10)
        self.record["candidate_container"] = identifiers[0]
        return identifiers[0]

    def finish_open(self):
        transaction = self.record["transaction"]
        # Commit before publishing the host-loopback Storage API. Once that port
        # is reachable, local clients may write even while Caddy is gated.
        self.save(phase="storage-opening", traffic_may_have_opened=True)
        self.traffic_may_have_opened = True
        self.verify_compose_artifacts()
        self.verify_image_refs()
        self.docker.compose(
            self.record["target_ref"], "up", "-d", "--no-deps", "--pull", "never",
            "--wait", "--wait-timeout", "240", APP_SERVICE,
            file=self.candidate_compose, timeout=300,
        )
        identifiers = compose_container_ids(
            self.docker, self.record["target_ref"], APP_SERVICE, self.candidate_compose
        )
        if len(identifiers) != 1:
            raise DeploymentError("The committed candidate did not start with Storage published.")
        candidate = self.docker.inspect(identifiers[0])
        check_service_container(candidate, image_id=self.record["target_image_id"], running=True)
        check_candidate_engine_settings(candidate)
        check_storage_binding(candidate)
        if candidate.get("State", {}).get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("The committed candidate did not become healthy with Storage published.")
        self.docker.run(["docker", "exec", identifiers[0], "bun", "-e", HEALTH_SCRIPT], timeout=10)
        if not storage_port_open():
            raise DeploymentError("The committed Storage loopback listener is not reachable.")
        self.record["candidate_container"] = identifiers[0]
        self.save(phase="storage-ready", traffic_may_have_opened=True)
        if self.ingress:
            self.ingress.assert_gated()

        # The journal is durable before this unlink. If a process dies after the
        # gate opens, recovery must never restore the now-stale snapshot.
        self.save(phase="opening", traffic_may_have_opened=True)
        disable_maintenance(transaction)
        self.traffic_may_have_opened = True
        self.save(phase="opened", traffic_may_have_opened=True)
        try:
            if self.ingress:
                self.ingress.assert_healthy(require_storage=True)
        except Exception:
            # Re-gate and recover the candidate, never roll back data after opening.
            enable_maintenance(transaction)
            self.save(phase="committed", traffic_may_have_opened=True)
            raise
        self.complete(
            self.record["target_ref"], self.record["target_image_id"],
            source=self.record["source_image"], migration="codex-credentials",
        )

    def recover_committed(self):
        transaction = self.record["transaction"]
        self.traffic_may_have_opened = True
        try:
            enable_maintenance(transaction)
            if self.ingress:
                self.ingress.assert_gated()
            # A visible commit phase is not proof that its directory entry made
            # it to stable storage. Persist it again before starting/publishing.
            self.save(phase="committed", traffic_may_have_opened=True)
            self.verify_compose_artifacts()
            self.verify_image_refs()
            self.stop_helpers()
            self.assert_single_transaction_writer({self.record["old_image_id"], self.record["target_image_id"]})
            # The application may have served requests already. Recreate/start only
            # the committed candidate; restoring the old snapshot is forbidden.
            self.docker.compose(
                self.record["target_ref"], "up", "-d", "--no-deps", "--pull", "never",
                "--wait", "--wait-timeout", "240", APP_SERVICE,
                file=self.candidate_compose, timeout=300,
            )
            candidate_ids = compose_container_ids(
                self.docker, self.record["target_ref"], APP_SERVICE, self.candidate_compose
            )
            if len(candidate_ids) != 1:
                raise DeploymentError("The committed candidate container is missing or ambiguous.")
            candidate = self.docker.inspect(candidate_ids[0])
            check_service_container(candidate, image_id=self.record["target_image_id"])
            if not candidate.get("State", {}).get("Running"):
                self.docker.run(["docker", "start", candidate_ids[0]], timeout=60)
            candidate = wait_healthy(self.docker, candidate_ids[0], self.record["target_image_id"])
            check_candidate_engine_settings(candidate)
            check_storage_binding(candidate)
            self.docker.run(["docker", "exec", candidate_ids[0], "bun", "-e", HEALTH_SCRIPT], timeout=10)
            if not storage_port_open():
                raise DeploymentError("The committed Storage loopback listener is not reachable.")
            disable_maintenance(transaction)
            self.ingress.assert_healthy(require_storage=True)
            self.complete(
                self.record["target_ref"], self.record["target_image_id"],
                source=self.record["source_image"], migration="codex-credentials",
            )
        except Exception:
            try:
                enable_maintenance(transaction)
                self.save(phase="committed-recovery-needed", traffic_may_have_opened=True)
            except Exception:
                pass
            raise DeploymentError("Committed candidate recovery failed; snapshot rollback is prohibited. Maintenance remains enabled.") from None


def cleanup_transaction_directory(transaction):
    directory = STATE / "transactions" / transaction
    if directory.is_symlink() or not directory.exists():
        return
    allowed = {
        "compose.final.yml", "compose.stage.yml", "compose.rollback.stage.yml",
        "runtime.env", "post-migration.tar",
    }
    try:
        for child in directory.iterdir():
            if child.name not in allowed or child.is_symlink() or not child.is_file():
                return
            child.unlink()
        directory.rmdir()
        sync_directory(directory.parent)
    except OSError:
        # These generated files contain runtime overrides; their directory remains
        # private if cleanup cannot be completed.
        return


def active_completion_matches(record):
    try:
        check_private_file(ACTIVE, required=True)
        active = json.loads(ACTIVE.read_text(encoding="utf-8"))
    except (DeploymentError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(active, dict)
        and active.get("status") == "complete"
        and active.get("transaction") == record.get("transaction")
        and active.get("backup") == record.get("backup")
        and (active.get("imageId"), active.get("image")) in {
            (record.get("target_image_id"), record.get("target_ref")),
            (record.get("old_image_id"), record.get("old_ref")),
        }
    )


def cleanup_completed_transaction(record):
    """Best-effort cleanup after a durable success marker; never alter traffic."""
    if not active_completion_matches(record) or maintenance_present():
        return False
    try:
        clear_pending()
    except BaseException:
        # If the journal unlink or its directory fsync is uncertain, retain the
        # recovery inputs in case a crash restores the journal entry.
        return True
    try:
        cleanup_transaction_directory(record["transaction"])
    except BaseException:
        pass
    return True


def read_pending():
    check_private_file(PENDING, required=True)
    try:
        record = json.loads(PENDING.read_text(encoding="utf-8"))
        required = {
            "transaction", "source_image", "target_ref", "target_image_id", "old_ref",
            "old_image_id", "backup", "phase", "candidate_compose", "data_may_change",
            "previous_compose", "previous_compose_sha256", "candidate_compose_sha256",
            "candidate_stage_compose", "candidate_stage_compose_sha256",
            "rollback_stage_compose", "rollback_stage_compose_sha256",
            "candidate_runtime_sha256", "maintenance_before", "proxy_image_ref",
            "rollback_compose_hashes",
        }
        if not isinstance(record, dict) or not required.issubset(record):
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{32}", record["transaction"]):
            raise ValueError
        if (not re.fullmatch(r"sha256:[0-9a-f]{64}", record["target_image_id"])
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["old_image_id"])
                or record["target_ref"] != image_reference(record["target_image_id"], "rawroute")
                or record["old_ref"] != image_reference(record["old_image_id"], "rollback")
                or not immutable(record["source_image"])
                or record["proxy_image_ref"] != PROXY_IMAGE):
            raise ValueError
        compose_hashes = record["rollback_compose_hashes"]
        if (not isinstance(compose_hashes, dict)
                or set(compose_hashes) != {"application", "stage", "proxy"}
                or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in compose_hashes.values())):
            raise ValueError
        phases = {
            "preflight-complete", "maintenance", "old-stopped", "snapshot-complete",
            "migration-preview", "migration-intent", "migration-complete",
            "candidate-boot-intent", "candidate-ready", "storage-opening", "storage-ready",
            "opening", "opened", "committed",
            "committed-recovery-needed", "rollback", "rollback-old-start", "rollback-storage-opening",
            "rollback-storage-ready", "rollback-opening", "rollback-opened",
            "rollback-committed", "rollback-failed", "rollback-committed-recovery-needed",
        }
        if record["phase"] not in phases or not isinstance(record["data_may_change"], bool):
            raise ValueError
        return record
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise DeploymentError("The deployment journal is invalid; do not run another deployment.") from None


def find_ingress(docker, image):
    identifiers = compose_container_ids(docker, image, PROXY_SERVICE, all_containers=False)
    if len(identifiers) != 1:
        raise DeploymentError("Expected one running Caddy ingress container.")
    proxy = docker.inspect(identifiers[0])
    return Ingress(docker, proxy)


def preflight(docker, image, *, transaction, backup_dir, candidate_stage_compose,
              candidate_compose, rollback_stage_compose):
    if not immutable(image):
        raise DeploymentError("Use an immutable :sha-<commit> tag or @sha256:<digest> image.")
    env_path = ROOT / ".env"
    try:
        env_info = env_path.lstat()
    except OSError:
        raise DeploymentError("The deployment .env file is missing.") from None
    if not stat.S_ISREG(env_info.st_mode):
        raise DeploymentError("The deployment .env file is missing.")
    regular_file(ROLLBACK_COMPOSE, "previous Compose")
    if PENDING.exists() or PENDING.is_symlink():
        raise DeploymentError("An interrupted deployment exists. Recover it before deploying again.")
    if maintenance_present():
        raise DeploymentError("A maintenance gate exists without a matching recovery journal.")
    docker.run(["docker", "pull", image], timeout=3600)
    target_image_id = docker.image_id(image)
    target_ref = image_reference(target_image_id, "rawroute")
    docker.tag(image, target_ref)
    image_config = docker.inspect(target_ref, image=True).get("Config") or {}
    tag_revision = re.search(r":sha-([0-9a-f]{40})$", image)
    labels = image_config.get("Labels") or {}
    if tag_revision and labels.get("org.opencontainers.image.revision") != tag_revision.group(1):
        raise DeploymentError("The published image revision does not match its full commit tag.")
    if any(retired_engine_setting(item.partition("=")[0]) for item in image_config.get("Env") or []):
        raise DeploymentError("The published candidate image contains retired engine settings.")
    docker.helper_preflight(target_ref, backup_dir)
    docker.compose(target_ref, "config", "--quiet", file=COMPOSE)
    docker.compose(target_ref, "config", "--quiet", file=candidate_stage_compose)
    docker.compose(target_ref, "config", "--quiet", file=candidate_compose)

    current_ids = compose_container_ids(docker, target_ref, APP_SERVICE, COMPOSE)
    if len(current_ids) != 1:
        raise DeploymentError("The production deployment must have exactly one existing application container.")
    old = docker.inspect(current_ids[0])
    check_service_container(old, running=True)
    check_storage_binding(old)
    if old.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise DeploymentError("The existing production application is not healthy.")
    volume_users = docker.volume_users()
    if volume_users != [current_ids[0]]:
        raise DeploymentError("Another running container uses the production data volume.")
    old_image_id = old.get("Image", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", old_image_id):
        raise DeploymentError("The existing production image ID is invalid.")
    old_ref = image_reference(old_image_id, "rollback")
    docker.tag(old_image_id, old_ref)
    docker.compose(old_ref, "config", "--quiet", file=ROLLBACK_COMPOSE)
    docker.compose(old_ref, "config", "--quiet", file=rollback_stage_compose)
    try:
        running_ref = (old.get("Config", {}).get("Image") or "").strip()
    except AttributeError:
        running_ref = ""
    if not running_ref:
        raise DeploymentError("The existing production image reference is missing.")

    ingress = find_ingress(docker, target_ref)
    check_private_compose_network(docker, old, ingress.proxy)
    proxy_image_ref = ((ingress.proxy.get("Config", {}).get("Image") or "").strip())
    if proxy_image_ref != PROXY_IMAGE:
        raise DeploymentError("The existing ingress image does not match the preserved Compose configuration.")
    old_labels = (old.get("Config", {}).get("Labels") or {})
    proxy_labels = (ingress.proxy.get("Config", {}).get("Labels") or {})
    live_app_hash = compose_service_hash(docker, running_ref, ROLLBACK_COMPOSE, APP_SERVICE)
    live_proxy_hash = compose_service_hash(docker, old_ref, ROLLBACK_COMPOSE, PROXY_SERVICE)
    if (old_labels.get("com.docker.compose.config-hash") != live_app_hash
            or proxy_labels.get("com.docker.compose.config-hash") != live_proxy_hash):
        raise DeploymentError("The rollback Compose files do not exactly match the running application and ingress.")
    rollback_compose_hashes = {
        "application": compose_service_hash(docker, old_ref, ROLLBACK_COMPOSE, APP_SERVICE),
        "stage": compose_service_hash(docker, old_ref, rollback_stage_compose, APP_SERVICE),
        "proxy": live_proxy_hash,
    }
    if compose_service_hash(docker, old_ref, ROLLBACK_COMPOSE, PROXY_SERVICE) != rollback_compose_hashes["proxy"]:
        raise DeploymentError("The rollback ingress Compose fingerprint is inconsistent.")
    ingress.assert_healthy(require_storage=True)
    return {
        "transaction": transaction,
        "source_image": image,
        "target_ref": target_ref,
        "target_image_id": target_image_id,
        "old_ref": old_ref,
        "old_source_image": running_ref,
        "proxy_image_ref": proxy_image_ref,
        "rollback_compose_hashes": rollback_compose_hashes,
        "previous_compose": str(ROLLBACK_COMPOSE),
        "previous_compose_sha256": private_file_digest(ROLLBACK_COMPOSE),
        "candidate_source_compose_sha256": private_file_digest(COMPOSE),
        "candidate_compose_sha256": private_file_digest(candidate_compose),
        "candidate_stage_compose_sha256": private_file_digest(candidate_stage_compose),
        "rollback_stage_compose_sha256": private_file_digest(rollback_stage_compose),
        "candidate_runtime_sha256": private_file_digest(candidate_compose.parent / "runtime.env"),
        "maintenance_before": False,
        "old_image_id": old_image_id,
        "old_container": current_ids[0],
        "backup": "",
        "phase": "preflight-complete",
        "snapshot_complete": False,
        "data_may_change": False,
        "candidate_compose": str(candidate_compose),
        "candidate_stage_compose": str(candidate_stage_compose),
        "rollback_stage_compose": str(rollback_stage_compose),
        "candidate_container": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, ingress


def execute_transaction(docker, image, transaction):
    safe_directory(STATE, 0o700, create=True)
    safe_directory(CONTROL, 0o755, create=True)
    safe_directory(STATE / "transactions", 0o700, create=True)
    safe_directory(BACKUPS, 0o700, create=True)
    transaction_dir = STATE / "transactions" / transaction
    backup_dir = None
    candidate_stage_compose = None
    candidate_compose = None
    rollback_stage_compose = None
    record = None
    tx = None
    gate_removed = False
    operation = "prepare transaction artifacts"
    last_completed_operation = "none"

    try:
        transaction_dir.mkdir(mode=0o700)
        sync_directory(transaction_dir.parent)
        candidate_stage_compose, candidate_compose, rollback_stage_compose = make_candidate_compose(transaction_dir, transaction)
        backup_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + transaction
        backup_dir = BACKUPS / backup_name
        backup_dir.mkdir(mode=0o700)
        sync_directory(BACKUPS)
        migration_backup_dir = backup_dir / "migration"
        migration_backup_dir.mkdir(mode=0o700)
        sync_directory(backup_dir)
        backup = backup_dir / "volume.tar"
        operation = "preflight existing deployment"
        record, ingress = preflight(
            docker, image, transaction=transaction, backup_dir=migration_backup_dir,
            candidate_stage_compose=candidate_stage_compose, candidate_compose=candidate_compose,
            rollback_stage_compose=rollback_stage_compose,
        )
        last_completed_operation = operation
        record.update({"backup": str(backup)})
        tx = Transaction(docker, record, candidate_stage_compose, candidate_compose,
                         rollback_stage_compose, ingress)
        operation = "persist initial journal"
        save_json(PENDING, record)
        last_completed_operation = operation

        operation = "enable and verify maintenance gate"
        enable_maintenance(transaction)
        tx.save(phase="maintenance")
        ingress.assert_gated()
        tx.save(phase="maintenance", gate_verified=True)
        last_completed_operation = operation
        operation = "stop prior application and verify idle volume"
        docker.run(["docker", "stop", "--time", "60", record["old_container"]], timeout=75)
        old_after_stop = docker.inspect(record["old_container"])
        check_service_container(old_after_stop, image_id=record["old_image_id"], running=False)
        tx.assert_no_writers()
        if storage_port_open():
            raise DeploymentError("The loopback Storage port remained reachable after the old writer stopped.")
        tx.save(phase="old-stopped")
        last_completed_operation = operation

        operation = "snapshot stopped volume"
        docker.snapshot(record["target_image_id"], VOLUME, backup, transaction, helper_name(transaction, "snapshot"))
        last_completed_operation = operation
        operation = "verify stopped-volume snapshot"
        snapshot = verify_snapshot(backup)
        sync_directory(backup_dir)
        tx.save(phase="snapshot-complete", snapshot_complete=True, snapshot=snapshot)
        last_completed_operation = operation

        operation = "preview Codex migration"
        before = docker.importer_preview(
            record["target_image_id"], backup, migration_backup_dir, transaction,
            helper_name(transaction, "codex-preview"),
        )
        if not isinstance(before.get("required"), bool) or not isinstance(before.get("accounts"), int) or not isinstance(before.get("resetRecords"), int):
            raise DeploymentError("The Codex importer preview did not return the expected plan.")
        tx.save(phase="migration-preview", migration={"required": before["required"], "accounts": before["accounts"], "resetRecords": before["resetRecords"]})
        last_completed_operation = operation
        if before["required"]:
            tx.save(phase="migration-intent", data_may_change=True)
            operation = "apply Codex migration"
            applied = docker.importer(record["target_image_id"], VOLUME, migration_backup_dir, transaction,
                                      helper_name(transaction, "codex-apply"), apply=True)
            last_completed_operation = operation
            # Validate the exact post-apply database state, not the pre-migration
            # backup. The stopped production volume is snapshotted read-only and
            # the planner runs against a disposable anonymous volume copy.
            validation_snapshot = transaction_dir / "post-migration.tar"
            tx.assert_no_writers()
            operation = "snapshot post-migration volume"
            docker.snapshot(
                record["target_image_id"], VOLUME, validation_snapshot, transaction,
                helper_name(transaction, "post-migration-snapshot"),
            )
            last_completed_operation = operation
            operation = "validate post-migration state"
            verify_snapshot(validation_snapshot)
            after = docker.importer_preview(
                record["target_image_id"], validation_snapshot, migration_backup_dir, transaction,
                helper_name(transaction, "codex-validate"),
            )
            validation_snapshot.unlink()
            sync_directory(transaction_dir)
            if (applied.get("applied") is not True or applied.get("accounts") != before["accounts"]
                    or applied.get("resetRecords") != before["resetRecords"]
                    or after.get("required") is not False or after.get("accounts") != 0 or after.get("resetRecords") != 0):
                raise DeploymentError("The Codex migration result did not match its stopped-writer preview.")
            last_completed_operation = operation
        operation = "boot candidate without Storage"
        tx.save(phase="migration-complete")

        # Starting a new app may itself migrate or write SQLite, even when the
        # explicit Codex conversion was already current.
        tx.save(phase="candidate-boot-intent", data_may_change=True)
        candidate_id = tx.candidate_ready()
        tx.save(phase="candidate-ready", candidate_container=candidate_id)
        last_completed_operation = operation
        ingress.assert_gated()
        operation = "commit and reopen ingress"
        tx.finish_open()
        gate_removed = tx.traffic_may_have_opened
        return 0
    except BaseException as error:
        if tx is None and record is not None:
            tx = Transaction(docker, record, candidate_stage_compose, candidate_compose,
                             rollback_stage_compose, None)
        if tx is not None and tx.completed:
            return 0
        failure_phase = record.get("phase") if isinstance(record, dict) else "startup"
        recovery_outcome = "not-required"
        if record and record.get("phase") == "opening" and not maintenance_present():
            gate_removed = True
            if tx:
                tx.traffic_may_have_opened = True
        if tx is not None:
            phase = record.get("phase")
            previous_opened = phase in {
                "rollback-old-start", "rollback-storage-opening", "rollback-storage-ready", "rollback-opening",
                "rollback-opened", "rollback-committed", "rollback-committed-recovery-needed",
            }
            candidate_opened = phase in {"storage-opening", "storage-ready", "opened", "committed", "committed-recovery-needed"}
            if phase == "opening" and not maintenance_present():
                candidate_opened = True
            if previous_opened:
                recovery_outcome = "intervention-required"
                try:
                    tx.recover_previous_committed()
                    recovery_outcome = "recovered"
                except BaseException:
                    recovery_outcome = "intervention-required"
            elif tx.traffic_may_have_opened or gate_removed or candidate_opened:
                recovery_outcome = "intervention-required"
                try:
                    tx.recover_committed()
                    recovery_outcome = "recovered"
                except BaseException:
                    recovery_outcome = "intervention-required"
            else:
                recovery_outcome = "intervention-required"
                try:
                    tx.rollback_preopen()
                    recovery_outcome = "recovered"
                except BaseException:
                    recovery_outcome = "intervention-required"
        else:
            cleanup_transaction_directory(transaction)
        report_failure(
            error, phase=failure_phase, operation=operation,
            last_completed=last_completed_operation, recovery=recovery_outcome,
        )
        raise


def recover(docker):
    try:
        record = read_pending()
    except BaseException as error:
        report_failure(
            error, phase="startup", operation="read recovery journal",
            last_completed="none", recovery="intervention-required",
        )
        raise
    phase = record.get("phase")
    try:
        recover_record(docker, record)
    except BaseException as error:
        report_failure(
            error, phase=phase, operation="resume interrupted deployment",
            last_completed="read recovery journal", recovery="intervention-required",
        )
        raise


def recover_record(docker, record):
    transaction = record["transaction"]
    candidate_compose = Path(record["candidate_compose"])
    candidate_stage_compose = Path(record["candidate_stage_compose"])
    rollback_stage_compose = Path(record["rollback_stage_compose"])
    expected_directory = STATE / "transactions" / transaction
    if (candidate_compose != expected_directory / "compose.final.yml"
            or candidate_stage_compose != expected_directory / "compose.stage.yml"
            or rollback_stage_compose != expected_directory / "compose.rollback.stage.yml"
            or candidate_compose.is_symlink() or candidate_stage_compose.is_symlink()
            or rollback_stage_compose.is_symlink()
            or not candidate_compose.is_file() or not candidate_stage_compose.is_file()
            or not rollback_stage_compose.is_file()):
        raise DeploymentError("The candidate Compose record needed for recovery is missing.")
    backup = Path(record["backup"])
    if (backup.name != "volume.tar" or backup.parent.parent != BACKUPS
            or not backup.parent.name.endswith("-" + transaction)):
        raise DeploymentError("The recovery backup path is outside this transaction's private backup directory.")
    if record.get("maintenance_before") is not False:
        raise DeploymentError("The journal's pre-deployment ingress state is unsupported.")
    proxy_image = record["target_ref"]
    ingress = find_ingress(docker, proxy_image)
    tx = Transaction(docker, record, candidate_stage_compose, candidate_compose,
                     rollback_stage_compose, ingress)
    phase = record.get("phase")
    if phase in {"rollback-old-start", "rollback-storage-opening", "rollback-storage-ready", "rollback-opening",
                 "rollback-opened", "rollback-committed", "rollback-committed-recovery-needed"}:
        tx.recover_previous_committed()
        return
    if phase in {"storage-opening", "storage-ready", "opening", "opened", "committed", "committed-recovery-needed"}:
        tx.recover_committed()
        return
    tx.rollback_preopen(gate_already_active=maintenance_present())


def prepare_runtime_state():
    safe_directory(STATE, 0o700, create=True)
    safe_directory(CONTROL, 0o755, create=True)
    safe_directory(STATE / "transactions", 0o700, create=True)
    safe_directory(BACKUPS, 0o700, create=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="Published image with a :sha-<commit> tag or @sha256:<digest>")
    parser.add_argument("--apply", action="store_true", help="Run the stopped-writer Codex migration transaction")
    parser.add_argument("--recover", action="store_true", help="Recover the pending deployment; requires --apply")
    args = parser.parse_args(argv)
    if args.recover and args.image:
        parser.error("--recover does not accept an image")
    if not args.recover and (not args.image or not immutable(args.image)):
        parser.error("Provide an immutable :sha-<commit> or @sha256:<digest> image")
    if args.recover and not args.apply:
        print("PREVIEW ONLY: --recover --apply will read the journal and either restore the stopped-volume backup and old image, or resume a committed candidate. No commands executed.")
        return 0
    if not args.apply:
        print("PREVIEW ONLY: no commands executed and no files created.")
        print(f"Pre-pull and pin {args.image}; gate Caddy; stop the only /data writer; verify a private full-volume backup; preview/apply/re-preview the Codex importer; health-check the candidate with Storage unpublished; then commit, publish loopback Storage, and reopen ingress.")
        print("Before the durable commit, failures restore the matching old image/config and, if data may have changed, the verified full-volume backup. A committed candidate is never rolled back to stale data.")
        return 0

    prepare_runtime_state()
    lock_path = STATE / "deploy.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(descriptor)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) & 0o077):
            raise DeploymentError("The deployment lock file is not private and owned by this user.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError("Another deployment or recovery holds the lock.") from None
        docker = Docker()
        if args.recover:
            recover(docker)
        else:
            transaction = uuid.uuid4().hex
            execute_transaction(docker, args.image, transaction)
    finally:
        os.close(descriptor)
    print("Application health and ingress verified; maintenance disabled.")
    if PENDING.exists():
        print("Journal cleanup remains; run `python3 deploy.py --recover --apply` before another deployment.")
    return 0


def cli(argv=None):
    """CLI boundary that reports only sanitized diagnostics, never tracebacks."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    recovery = "intervention-required" if "--recover" in arguments else "not-required"
    try:
        return main(arguments)
    except SystemExit:
        raise
    except KeyboardInterrupt as error:
        if not getattr(error, "_rawroute_diagnostic_reported", False):
            report_failure(
                error, phase="startup", operation="start deployment",
                last_completed="none", recovery=recovery,
            )
        return 1
    except Exception as error:
        if not getattr(error, "_rawroute_diagnostic_reported", False):
            report_failure(
                error, phase="startup", operation="start deployment",
                last_completed="none", recovery=recovery,
            )
        return 1


if __name__ == "__main__":
    sys.exit(cli())
