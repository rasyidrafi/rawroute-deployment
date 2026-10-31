import contextlib
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

import deploy

TARGET_ID = "sha256:" + "b" * 64
OLD_ID = "sha256:" + "a" * 64
IMAGE = "docker.io/example/rawroute:sha-" + "b" * 40
TRANSACTION = "1" * 32
OLD_REF = deploy.image_reference(OLD_ID, "rollback")
TARGET_REF = deploy.image_reference(TARGET_ID, "rawroute")
LIVE_APP_HASH = "1" * 64
ROLLBACK_APP_HASH = "2" * 64
ROLLBACK_STAGE_HASH = "3" * 64
PROXY_HASH = "4" * 64


def fake_snapshot(path, secret=b"synthetic-codex-token"):
    with tarfile.open(path, "w") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        db = tarfile.TarInfo("./rawroute.sqlite")
        db.size = 14
        db.mode = 0o600
        archive.addfile(db, io.BytesIO(b"synthetic-db!!"))
        cliproxy = tarfile.TarInfo("./cliproxy")
        cliproxy.type = tarfile.DIRTYPE
        archive.addfile(cliproxy)
        auth = tarfile.TarInfo("./cliproxy/auth")
        auth.type = tarfile.DIRTYPE
        archive.addfile(auth)
        config = tarfile.TarInfo("./cliproxy/config.yaml")
        config.size = 18
        config.mode = 0o600
        archive.addfile(config, io.BytesIO(b"config-version: 8\n"))
        token = tarfile.TarInfo("./cliproxy/auth/fake.json")
        token.size = len(secret)
        token.mode = 0o600
        archive.addfile(token, io.BytesIO(secret))
    os.chmod(path, 0o600)
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def base_snapshot_entries():
    return [
        (".", "dir", None, 0o755),
        ("rawroute.sqlite", "file", b"synthetic-sqlite", 0o600),
        ("cliproxy", "dir", None, 0o755),
        ("cliproxy/auth", "dir", None, 0o700),
        ("cliproxy/config.yaml", "file", b"config-version: 8\n", 0o600),
    ]


def synthetic_snapshot(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, kind, value, mode in entries:
            member = tarfile.TarInfo(name)
            member.mode = mode
            if kind == "dir":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "file":
                member.type = tarfile.REGTYPE
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = value
                archive.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
                archive.addfile(member)
            elif kind == "char":
                member.type = tarfile.CHRTYPE
                member.devmajor = 1
                member.devminor = 3
                archive.addfile(member)
            else:
                raise AssertionError(f"unsupported synthetic archive type: {kind}")
    os.chmod(path, 0o600)
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def app_container(identifier, image_id, *, running=True, health="healthy", storage_ip="127.0.0.1",
                  exposed=True, config_hash=None):
    ports = [{"HostIp": storage_ip, "HostPort": "9000"}]
    return {
        "Id": identifier,
        "Image": image_id,
        "Name": "/rawroute-production-rawroute-1",
        "Config": {
            "Image": "docker.io/example/rawroute:old" if image_id == OLD_ID else TARGET_REF,
            "Labels": {
                "com.docker.compose.project": deploy.PROJECT,
                "com.docker.compose.service": deploy.APP_SERVICE,
                "com.docker.compose.config-hash": config_hash or (LIVE_APP_HASH if image_id == OLD_ID else "b" * 64),
            },
        },
        "Mounts": [{"Type": "volume", "Name": deploy.VOLUME, "Destination": "/data", "RW": True}],
        "HostConfig": {
            "PortBindings": {"9000/tcp": copy.deepcopy(ports)} if exposed else {},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "NetworkSettings": {
            "Ports": {"9000/tcp": copy.deepcopy(ports)} if exposed else {},
            "Networks": {"rawroute-production_default": {}},
        },
        "State": {"Running": running, "Health": {"Status": health}},
    }


def proxy_container(control):
    return {
        "Id": "proxy-id",
        "Name": "/rawroute-production-proxy-1",
        "Config": {"Image": deploy.PROXY_IMAGE, "Env": ["RAWROUTE_SITE=rawroute.example.test"],
                   "Labels": {"com.docker.compose.project": deploy.PROJECT,
                              "com.docker.compose.service": deploy.PROXY_SERVICE,
                              "com.docker.compose.config-hash": PROXY_HASH}},
        "HostConfig": {"PortBindings": {"443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8443"}]}},
        "Mounts": [{"Type": "bind", "Source": str(control), "Destination": "/srv/control", "RW": False}],
        "NetworkSettings": {"Networks": {"rawroute-production_default": {}}},
        "State": {"Running": True},
    }


class FakeIngress:
    def __init__(self, events, control):
        self.events = events
        self.control = control
        self.fail_open_once = False
        self.healthy_count = 0
        self.fail_gate = False

    def assert_gated(self):
        self.events.append("gate-probe")
        if self.fail_gate or not (self.control / "maintenance").exists():
            raise deploy.DeploymentError("synthetic ingress probe failure")

    def assert_healthy(self, *, require_storage=True):
        self.events.append("public-health")
        self.healthy_count += 1
        if (self.control / "maintenance").exists():
            raise deploy.DeploymentError("synthetic maintenance response")
        if self.fail_open_once and self.healthy_count == 2:
            self.fail_open_once = False
            raise deploy.DeploymentError("synthetic public health failure")


class FakeDocker:
    def __init__(self, control):
        self.events = []
        self.control = control
        self.images = {IMAGE: TARGET_ID, TARGET_REF: TARGET_ID, OLD_ID: OLD_ID, OLD_REF: OLD_ID}
        self.containers = {
            "old-id": app_container("old-id", OLD_ID),
            "proxy-id": proxy_container(control),
        }
        self.active = "old-id"
        self.volume_data = {"database": "before", "storage-key": "synthetic-storage-key", "other": "preserved"}
        self.snapshot_data = None
        self.snapshots = {}
        self.preview_snapshots = []
        self.migration_required = True
        self.preview_count = 0
        self.fail = None
        self.bad_loopback = False
        self.hash_mismatch = False
        self.ingress = FakeIngress(self.events, control)
        self.private_stderr = "synthetic-auth-token-must-not-be-logged"
        self.storage_state = {"open": True}

    def run(self, args, *, capture=False, timeout=1800, stdout=None, env=None):
        if args[:2] == ["docker", "pull"]:
            self.events.append("pull")
        elif args[:3] == ["docker", "stop", "--time"]:
            identifier = args[-1]
            self.events.append("stop-old" if identifier == "old-id" else "stop-candidate")
            self.containers[identifier]["State"]["Running"] = False
            if identifier in {"old-id", "candidate-id"}:
                self.storage_state["open"] = False
        elif args[:2] == ["docker", "stop"]:
            identifier = args[-1]
            self.events.append("stop-helper")
            self.containers[identifier]["State"]["Running"] = False
        elif args[:2] == ["docker", "update"]:
            self.events.append("update-restart")
        elif args[:2] == ["docker", "exec"]:
            self.events.append("deep-health")
        elif args[:2] == ["docker", "start"]:
            self.events.append("start-candidate")
            self.containers[args[-1]]["State"]["Running"] = True
        elif args[:2] == ["docker", "rm"]:
            self.events.append("remove-helper")
        return "" if capture else None

    def compose(self, image, *args, file=deploy.COMPOSE, capture=False, timeout=1800):
        if args[:2] == ("config", "--quiet"):
            self.events.append("config-candidate" if file != deploy.ROLLBACK_COMPOSE else "config-rollback")
            return "" if capture else None
        if args[:2] == ("config", "--hash"):
            service = args[-1]
            if service == deploy.PROXY_SERVICE:
                digest = PROXY_HASH
            elif Path(file).name == "compose.rollback.stage.yml":
                digest = ROLLBACK_STAGE_HASH
            elif image == "docker.io/example/rawroute:old":
                digest = "f" * 64 if self.hash_mismatch else LIVE_APP_HASH
            else:
                digest = ROLLBACK_APP_HASH
            return f"{service} {digest}\n" if capture else None
        if args[:1] == ("ps",):
            if args[-1] == deploy.PROXY_SERVICE:
                return "proxy-id" if capture else None
            return self.active if capture else None
        if args[:1] == ("up",):
            rollback = file == deploy.ROLLBACK_COMPOSE or Path(file).name == "compose.rollback.stage.yml"
            if rollback:
                final = file == deploy.ROLLBACK_COMPOSE
                if final:
                    journal = json.loads(deploy.PENDING.read_text())
                    committed_phases = {
                        "rollback-storage-opening", "rollback-storage-ready", "rollback-opening",
                        "rollback-opened", "rollback-committed", "rollback-committed-recovery-needed",
                    }
                    if journal.get("phase") not in committed_phases:
                        raise AssertionError("Rollback Storage was published before its durable commit record")
                    self.events.append("commit-before-old-storage")
                self.events.append("start-old-final" if final else "start-old-stage")
                if self.fail == "old-boot":
                    raise deploy.DeploymentError("synthetic old boot failure")
                self.containers["old-id"] = app_container(
                    "old-id", OLD_ID, running=True, exposed=final,
                    config_hash=ROLLBACK_APP_HASH if final else ROLLBACK_STAGE_HASH,
                )
                self.active = "old-id"
                self.storage_state["open"] = final
            else:
                final = Path(file).name == "compose.final.yml"
                if final:
                    journal = json.loads(deploy.PENDING.read_text())
                    committed_phases = {
                        "storage-opening", "storage-ready", "opening", "opened", "committed",
                        "committed-recovery-needed", "rollback-storage-opening", "rollback-storage-ready",
                        "rollback-opening", "rollback-opened", "rollback-committed",
                        "rollback-committed-recovery-needed",
                    }
                    if journal.get("phase") not in committed_phases:
                        raise AssertionError("Storage was published before the durable commit record")
                    self.events.append("commit-before-storage")
                self.events.append("start-candidate-final" if final else "start-candidate-stage")
                self.volume_data["database"] = "candidate startup wrote database"
                candidate = app_container(
                    "candidate-id", TARGET_ID, running=True,
                    health="unhealthy" if self.fail == "candidate-boot" else "healthy",
                    storage_ip="0.0.0.0" if self.bad_loopback else "127.0.0.1",
                    exposed=final,
                    config_hash="b" * 64,
                )
                self.containers["candidate-id"] = candidate
                self.active = "candidate-id"
                self.storage_state["open"] = final
                if self.fail == "candidate-boot":
                    raise deploy.DeploymentError("synthetic candidate boot failure")
            return "" if capture else None
        raise AssertionError(f"Unexpected fake Compose operation: {args}")

    def inspect(self, resource, *, image=False):
        if image:
            if resource not in self.images:
                raise deploy.DeploymentError("synthetic missing image")
            return {"Id": self.images[resource], "Config": {
                "Labels": {"org.opencontainers.image.revision": "b" * 40},
            }}
        if resource == "rawroute-production_default":
            return {"Containers": {"old-id": {}, "proxy-id": {}}}
        return copy.deepcopy(self.containers[resource])

    def image_id(self, image):
        if image not in self.images:
            raise deploy.DeploymentError("synthetic missing image")
        return self.images[image]

    def tag(self, source, destination):
        self.images[destination] = self.images[source]
        self.events.append("pin-image")

    def volume_users(self):
        return [identifier for identifier, container in self.containers.items()
                if container.get("State", {}).get("Running")
                and any(mount.get("Name") == deploy.VOLUME for mount in container.get("Mounts", []))]

    def helper_preflight(self, image, backup_root):
        self.events.append("helper-preflight")

    def snapshot(self, image, volume, destination, transaction, helper):
        destination = Path(destination)
        self.events.append("snapshot-postapply" if destination.name == "post-migration.tar" else "snapshot")
        if (self.fail == "snapshot"
                or (self.fail == "post-snapshot" and destination.name == "post-migration.tar")):
            raise deploy.DeploymentError("synthetic snapshot failure")
        if self.fail == "secret-runtime-error":
            raise RuntimeError("synthetic-secret /private/auth/account-name payload=synthetic-token")
        if self.fail == "secret-deployment-error":
            raise deploy.DeploymentError("synthetic-secret /private/auth/account-name payload=synthetic-token")
        self.snapshot_data = copy.deepcopy(self.volume_data)
        self.snapshots[str(destination)] = copy.deepcopy(self.volume_data)
        if self.fail == "invalid-snapshot":
            synthetic_snapshot(destination, base_snapshot_entries() + [
                ("cliproxy/auth/private-link", "symlink", "missing", 0o777),
            ])
            return
        fake_snapshot(destination)

    def restore(self, image, volume, backup, transaction, helper):
        self.events.append("restore")
        if self.fail == "restore":
            raise deploy.DeploymentError("synthetic restore failure")
        self.volume_data = copy.deepcopy(self.snapshots.get(str(backup), self.snapshot_data))

    def importer_preview(self, image, snapshot, backup_root, transaction, helper):
        self.events.append("import-preview")
        self.preview_count += 1
        self.preview_snapshots.append(copy.deepcopy(self.snapshots.get(str(snapshot))))
        if self.fail == "preview":
            raise deploy.DeploymentError("Codex importer preview failed (exit 1).")
        if self.fail == "repreview" and self.preview_count > 1:
            return {"required": True, "accounts": 1, "resetRecords": 1}
        return {
            "required": self.migration_required if self.preview_count == 1 else False,
            "accounts": 2 if self.preview_count == 1 and self.migration_required else 0,
            "resetRecords": 1 if self.preview_count == 1 and self.migration_required else 0,
        }

    def importer(self, image, volume, backup_root, transaction, helper, *, apply):
        self.events.append("import-apply")
        if not apply:
            raise AssertionError("Production preview must use a disposable snapshot copy")
        if self.fail == "apply":
            raise deploy.DeploymentError("Codex importer apply failed (exit 1).")
        self.volume_data["database"] = "codex credentials migrated"
        return {"applied": True, "accounts": 2, "resetRecords": 1}

    def stop_helpers(self, transaction):
        self.events.append("stop-helpers")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / ".state"
        self.control = self.state / "control"
        self.transactions = self.state / "transactions"
        self.backups = self.root / "backups"
        self.state.mkdir(mode=0o700)
        self.control.mkdir(mode=0o755)
        self.transactions.mkdir(mode=0o700)
        self.backups.mkdir(mode=0o700)
        self.runtime_env = self.state / "runtime.env"
        self.runtime_env.write_text("PUBLIC_SETTING=value\nAPI_TOKEN=synthetic-secret\nCLIPROXY_MODE=managed\nCLI_PROXY_API_KEY=retired-secret\nMANAGEMENT_PASSWORD=retired-password\n")
        self.runtime_env.chmod(0o600)
        self.env = self.root / ".env"
        self.env.write_text("RAWROUTE_HOST=rawroute.example.test\n")
        self.compose_file = self.root / "compose.yml"
        self.compose_file.write_text(
            "services:\n  rawroute:\n    env_file:\n      - path: ./.state/runtime.env\n        required: false\n"
            "    environment:\n      STORAGE_ENABLED: \"true\"\n"
            "    ports:\n      - \"127.0.0.1:9000:9000\"\n"
        )
        self.rollback_file = self.root / "compose.rollback.yml"
        self.rollback_file.write_text(
            "services:\n  rawroute:\n    environment:\n      CLIPROXY_MODE: managed\n"
            "      STORAGE_ENABLED: \"true\"\n      STORAGE_S3_PORT: \"9000\"\n"
            "    ports:\n      - \"127.0.0.1:9000:9000\"\n"
        )
        replacements = {
            "ROOT": self.root,
            "STATE": self.state,
            "CONTROL": self.control,
            "PENDING": self.state / "pending.json",
            "ACTIVE": self.state / "active.json",
            "COMPOSE": self.compose_file,
            "ROLLBACK_COMPOSE": self.rollback_file,
            "RUNTIME_ENV": self.runtime_env,
            "BACKUPS": self.backups,
        }
        for name, value in replacements.items():
            patcher = patch.object(deploy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.docker = FakeDocker(self.control)
        self.real_ingress = deploy.Ingress
        def fake_ingress(_docker, proxy):
            self.docker.ingress.proxy = proxy
            return self.docker.ingress

        patcher = patch.object(deploy, "Ingress", side_effect=fake_ingress)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(deploy, "storage_port_open", side_effect=lambda: self.docker.storage_state["open"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_transaction(self, *, transaction=TRANSACTION):
        return deploy.execute_transaction(self.docker, IMAGE, transaction)

    def assert_old_healthy_and_restored(self):
        self.assertEqual(self.docker.active, "old-id")
        self.assertTrue(self.docker.containers["old-id"]["State"]["Running"])
        self.assertEqual(self.docker.containers["old-id"]["Image"], OLD_ID)
        self.assertEqual(self.docker.volume_data["database"], "before")
        self.assertEqual(self.docker.volume_data["storage-key"], "synthetic-storage-key")
        self.assertEqual(self.docker.volume_data["other"], "preserved")
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())

    def test_immutable_image_and_preview_are_read_only(self):
        self.assertTrue(deploy.immutable(IMAGE))
        self.assertTrue(deploy.immutable("registry.example:5000/app@sha256:" + "c" * 64))
        for value in ("app:latest", "app:v1", "app", "app:sha-abcdef0", "app:sha-main", "app@sha256:abc", "app;echo bad"):
            self.assertFalse(deploy.immutable(value))
        with patch.object(deploy, "Docker", side_effect=AssertionError("Docker must not be constructed")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(deploy.main([IMAGE]), 0)
                self.assertEqual(deploy.main(["--recover"]), 0)
        self.assertIn("PREVIEW ONLY", output.getvalue())
        self.assertEqual(set(self.state.iterdir()), {self.control, self.transactions, self.runtime_env})

    def test_candidate_runtime_env_drops_retired_settings_without_output(self):
        transaction_dir = self.transactions / TRANSACTION
        transaction_dir.mkdir(mode=0o700)
        stage, candidate, rollback_stage = deploy.make_candidate_compose(transaction_dir, TRANSACTION)
        sanitized = (transaction_dir / "runtime.env").read_text()
        self.assertIn("PUBLIC_SETTING=value", sanitized)
        self.assertIn("API_TOKEN=synthetic-secret", sanitized)
        self.assertNotIn("CLIPROXY", sanitized)
        self.assertNotIn("CLI_PROXY", sanitized)
        self.assertNotIn("MANAGEMENT_PASSWORD", sanitized)
        self.assertEqual(stat.S_IMODE((transaction_dir / "runtime.env").stat().st_mode), 0o600)
        self.assertIn(f".state/transactions/{TRANSACTION}/runtime.env", candidate.read_text())
        self.assertIn(f".state/transactions/{TRANSACTION}/runtime.env", stage.read_text())
        self.assertNotIn('"127.0.0.1:9000:9000"', stage.read_text())
        self.assertNotIn('"127.0.0.1:9000:9000"', rollback_stage.read_text())

    def test_verified_snapshot_includes_database_auth_and_config(self):
        path = self.backups / "fixture.tar"
        fake_snapshot(path)
        result = deploy.verify_snapshot(path)
        self.assertGreater(result["bytes"], 0)
        self.assertGreaterEqual(result["files"], 3)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_snapshot_rejects_missing_engine_config_and_unsafe_paths(self):
        path = self.backups / "bad.tar"
        with tarfile.open(path, "w") as archive:
            for name, kind, payload in (
                ("rawroute.sqlite", tarfile.REGTYPE, b"db"),
                ("cliproxy/auth", tarfile.DIRTYPE, b""),
                ("../escape", tarfile.REGTYPE, b"bad"),
            ):
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                else:
                    archive.addfile(member)
        path.chmod(0o600)
        with self.assertRaisesRegex(deploy.DeploymentError, "unsafe path"):
            deploy.verify_snapshot(path)
        self.assertFalse(deploy.check_archive_exists(path) is False)

    def test_snapshot_accepts_nested_directory_symlink_before_its_target(self):
        path = self.backups / "nested-link.tar"
        entries = base_snapshot_entries() + [
            ("cache", "dir", None, 0o750),
            ("cache/current", "symlink", "snapshots/active", 0o777),
            ("cache/snapshots", "dir", None, 0o750),
            ("cache/snapshots/active", "dir", None, 0o750),
            ("cache/snapshots/active/payload.bin", "file", b"synthetic payload", 0o640),
        ]
        synthetic_snapshot(path, entries)
        result = deploy.verify_snapshot(path)
        self.assertGreaterEqual(result["files"], 3)
        with tarfile.open(path, "r:") as archive:
            link = next(member for member in archive.getmembers() if member.name == "cache/current")
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, "snapshots/active")

    def test_snapshot_rejects_unsafe_structure_and_link_targets(self):
        base = base_snapshot_entries()
        cases = {
            "absolute member": base + [("/escape", "file", b"x", 0o600)],
            "parent traversal member": base + [("cache/../../escape", "file", b"x", 0o600)],
            "normalized duplicate": base + [("./rawroute.sqlite", "file", b"duplicate", 0o600)],
            "duplicate normalized root": base + [("./", "dir", None, 0o755)],
            "non-directory root": [(".", "file", b"not-root", 0o600)] + base[1:],
            "missing ancestor": base + [("missing/link", "symlink", "target", 0o777),
                                        ("missing/target", "dir", None, 0o755)],
            "file ancestor": base + [("cache", "file", b"not-directory", 0o600),
                                     ("cache/link", "symlink", "target", 0o777)],
            "symlink ancestor": base + [("real", "dir", None, 0o755),
                                        ("cache", "symlink", "real", 0o777),
                                        ("cache/child", "file", b"under-link", 0o600)],
            "absolute link target": base + [("cache", "dir", None, 0o755),
                                             ("cache/link", "symlink", "/outside", 0o777)],
            "parent traversal link target": base + [("cache", "dir", None, 0o755),
                                                     ("cache/link", "symlink", "../outside", 0o777)],
            "empty link target": base + [("cache", "dir", None, 0o755),
                                         ("cache/link", "symlink", "", 0o777)],
            "dangling link": base + [("cache", "dir", None, 0o755),
                                     ("cache/link", "symlink", "missing", 0o777)],
            "link to file": base + [("cache", "dir", None, 0o755),
                                    ("cache/payload", "file", b"not-directory", 0o600),
                                    ("cache/link", "symlink", "payload", 0o777)],
            "chained link target": base + [("cache", "dir", None, 0o755),
                                           ("cache/real", "dir", None, 0o755),
                                           ("cache/target", "symlink", "real", 0o777),
                                           ("cache/link", "symlink", "target", 0o777)],
            "cyclic parent target": base + [("cache", "dir", None, 0o755),
                                            ("cache/link", "symlink", ".", 0o777)],
            "target path traverses symlink": base + [
                ("cache", "dir", None, 0o755), ("cache/real", "dir", None, 0o755),
                ("cache/real/nested", "dir", None, 0o755),
                ("cache/route", "symlink", "real", 0o777),
                ("cache/link", "symlink", "route/nested", 0o777),
            ],
            "auth symlink": base + [("cliproxy/auth/target", "dir", None, 0o700),
                                    ("cliproxy/auth/alias", "symlink", "target", 0o777)],
            "hardlink": base + [("cache", "dir", None, 0o755),
                                ("cache/hard", "hardlink", "rawroute.sqlite", 0o600)],
            "fifo": base + [("cache", "dir", None, 0o755), ("cache/fifo", "fifo", None, 0o600)],
            "device": base + [("cache", "dir", None, 0o755), ("cache/device", "char", None, 0o600)],
            "auth hardlink": base + [("cliproxy/auth/hard", "hardlink", "rawroute.sqlite", 0o600)],
            "auth fifo": base + [("cliproxy/auth/fifo", "fifo", None, 0o600)],
        }
        for index, (label, entries) in enumerate(cases.items()):
            with self.subTest(label=label):
                path = self.backups / f"invalid-{index}.tar"
                synthetic_snapshot(path, entries)
                with self.assertRaises(deploy.DeploymentError):
                    deploy.verify_snapshot(path)

    def test_snapshot_rejects_descendants_below_link_in_any_tar_order(self):
        for link_first in (True, False):
            with self.subTest(link_first=link_first):
                path = self.backups / f"link-descendant-{link_first}.tar"
                link = ("cache/current", "symlink", "targets/active", 0o777)
                child = ("cache/current/payload", "file", b"must-not-be-followed", 0o600)
                entries = base_snapshot_entries() + [("cache", "dir", None, 0o755)]
                entries += [link, child] if link_first else [child, link]
                entries += [
                    ("cache/targets", "dir", None, 0o755),
                    ("cache/targets/active", "dir", None, 0o755),
                ]
                synthetic_snapshot(path, entries)
                with self.assertRaises(deploy.DeploymentError):
                    deploy.verify_snapshot(path)

    def test_snapshot_requires_database_config_and_auth_directory_to_be_regular_types(self):
        cases = (
            [entry for entry in base_snapshot_entries() if entry[0] != "rawroute.sqlite"]
            + [("cache", "dir", None, 0o755), ("rawroute.sqlite", "symlink", "cache", 0o777)],
            [entry for entry in base_snapshot_entries() if entry[0] != "cliproxy/config.yaml"]
            + [("cliproxy/config.yaml", "symlink", "auth", 0o777)],
            [entry for entry in base_snapshot_entries() if entry[0] != "cliproxy/auth"]
            + [("cliproxy/auth", "symlink", "config.yaml", 0o777)],
        )
        for index, entries in enumerate(cases):
            with self.subTest(index=index):
                path = self.backups / f"required-regular-{index}.tar"
                synthetic_snapshot(path, entries)
                with self.assertRaises(deploy.DeploymentError):
                    deploy.verify_snapshot(path)

    def test_invalid_archive_and_digest_never_reach_restore_or_target_cleanup(self):
        invalid = self.backups / "unsafe-restore.tar"
        synthetic_snapshot(invalid, base_snapshot_entries() + [
            ("cliproxy/auth/alias", "symlink", "target", 0o777),
            ("cliproxy/auth/target", "dir", None, 0o700),
        ])
        original = copy.deepcopy(self.docker.volume_data)
        tx = deploy.Transaction(self.docker, {
            "transaction": TRANSACTION, "data_may_change": True, "snapshot_complete": True,
            "backup": str(invalid), "snapshot": {"sha256": deploy.private_file_digest(invalid)},
            "old_image_id": OLD_ID, "target_image_id": TARGET_ID,
        }, None, None, None, None)
        with self.assertRaises(deploy.DeploymentError):
            tx.restore_volume_if_needed()
        self.assertNotIn("restore", self.docker.events)
        self.assertEqual(self.docker.volume_data, original)

        valid = self.backups / "digest-mismatch.tar"
        synthetic_snapshot(valid, base_snapshot_entries())
        tx.record.update({"backup": str(valid), "snapshot": {"sha256": "0" * 64}})
        with self.assertRaisesRegex(deploy.DeploymentError, "digest"):
            tx.restore_volume_if_needed()
        self.assertNotIn("restore", self.docker.events)

    def test_maintenance_gate_is_atomically_published_and_recovers_fsync_failure(self):
        marker = self.control / "maintenance"
        with patch.object(deploy.os, "link", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                deploy.enable_maintenance(TRANSACTION)
        self.assertFalse(marker.exists())

        with patch.object(deploy, "sync_directory", side_effect=OSError("synthetic directory fsync failure")):
            with self.assertRaisesRegex(deploy.DeploymentError, "durably published"):
                deploy.enable_maintenance(TRANSACTION)
        self.assertEqual(marker.read_text(), TRANSACTION)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        deploy.verify_maintenance_marker(TRANSACTION)
        deploy.enable_maintenance(TRANSACTION)
        with self.assertRaisesRegex(deploy.DeploymentError, "outside this deployment transaction"):
            deploy.enable_maintenance("2" * 32)

    def test_success_order_full_backup_migration_candidate_and_reopen(self):
        self.assertEqual(self.run_transaction(), 0)
        events = self.docker.events
        self.assertLess(events.index("pull"), events.index("gate-probe"))
        self.assertLess(events.index("stop-old"), events.index("snapshot"))
        self.assertLess(events.index("snapshot"), events.index("import-preview"))
        self.assertLess(events.index("import-preview"), events.index("import-apply"))
        self.assertLess(events.index("import-apply"), events.index("snapshot-postapply"))
        self.assertLess(events.index("snapshot-postapply"), events.index("import-preview", events.index("snapshot-postapply")))
        self.assertLess(events.index("import-apply"), events.index("start-candidate-stage"))
        self.assertLess(events.index("start-candidate-stage"), events.index("start-candidate-final"))
        self.assertLess(events.index("commit-before-storage"), events.index("start-candidate-final"))
        self.assertEqual(events.count("import-preview"), 2)
        self.assertEqual(self.docker.preview_snapshots[0]["database"], "before")
        self.assertEqual(self.docker.preview_snapshots[1]["database"], "codex credentials migrated")
        self.assertGreater(events.index("public-health", events.index("start-candidate-final")), events.index("start-candidate-final"))
        self.assertEqual(self.docker.volume_data["database"], "candidate startup wrote database")
        self.assertEqual(self.docker.active, "candidate-id")
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())
        active = json.loads(deploy.ACTIVE.read_text())
        self.assertEqual(active["imageId"], TARGET_ID)
        self.assertEqual(active["migration"], "codex-credentials")
        self.assertTrue(list(self.backups.glob("*/volume.tar")))

    def test_preview_failure_restarts_old_without_unneeded_snapshot_restore(self):
        self.docker.fail = "preview"
        with self.assertRaisesRegex(deploy.DeploymentError, "preview failed"):
            self.run_transaction()
        self.assertIn("snapshot", self.docker.events)
        self.assertNotIn("restore", self.docker.events)
        self.assert_old_healthy_and_restored()

    def test_apply_failure_restores_matching_full_volume_and_old_image_config(self):
        self.docker.fail = "apply"
        with self.assertRaisesRegex(deploy.DeploymentError, "apply failed"):
            self.run_transaction()
        self.assertIn("restore", self.docker.events)
        self.assert_old_healthy_and_restored()
        rollback_up = [event for event in self.docker.events if event == "start-old-final"]
        self.assertEqual(len(rollback_up), 1)

    def test_safe_failure_diagnostic_preserves_snapshot_cause_after_rollback(self):
        self.docker.fail = "invalid-snapshot"
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(deploy.DeploymentError, "unsupported symbolic link"):
                self.run_transaction()
        diagnostic = output.getvalue()
        self.assertIn("The legacy Codex auth backup contains an unsupported symbolic link.", diagnostic)
        self.assertIn("phase=old-stopped", diagnostic)
        self.assertIn("last-completed=snapshot stopped volume", diagnostic)
        self.assertIn("operation=verify stopped-volume snapshot", diagnostic)
        self.assertIn("recovery=recovered", diagnostic)
        self.assertNotIn("/", diagnostic)
        self.assert_old_healthy_and_restored()

    def test_secret_bearing_unhandled_errors_are_redacted_after_recovery(self):
        for failure in ("secret-runtime-error", "secret-deployment-error"):
            with self.subTest(failure=failure):
                if failure == "secret-deployment-error":
                    self.docker = FakeDocker(self.control)
                self.docker.fail = failure
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    with self.assertRaises((RuntimeError, deploy.DeploymentError)):
                        self.run_transaction()
                diagnostic = output.getvalue()
                self.assertNotIn("synthetic-secret", diagnostic)
                self.assertNotIn("account-name", diagnostic)
                self.assertNotIn("synthetic-token", diagnostic)
                self.assertNotIn("/private", diagnostic)
                self.assertIn("An unexpected deployment exception occurred.", diagnostic)
                self.assertIn("phase=old-stopped", diagnostic)
                self.assertIn("recovery=recovered", diagnostic)
                self.assert_old_healthy_and_restored()

    def test_cli_sanitizes_unhandled_exception_text(self):
        output = io.StringIO()
        with patch.object(deploy, "main", side_effect=RuntimeError("synthetic-secret /private/data token=payload")), \
                contextlib.redirect_stderr(output):
            self.assertEqual(deploy.cli([IMAGE, "--apply"]), 1)
        diagnostic = output.getvalue()
        self.assertNotIn("synthetic-secret", diagnostic)
        self.assertNotIn("/private/data", diagnostic)
        self.assertNotIn("payload", diagnostic)
        self.assertIn("An unexpected deployment exception occurred.", diagnostic)
        self.assertIn("recovery=not-required", diagnostic)

    def test_post_apply_validation_snapshot_is_fresh_and_failure_restores_original_snapshot(self):
        self.docker.fail = "post-snapshot"
        with self.assertRaisesRegex(deploy.DeploymentError, "snapshot failure"):
            self.run_transaction()
        self.assertIn("snapshot-postapply", self.docker.events)
        self.assertNotIn("start-candidate-stage", self.docker.events)
        self.assertEqual(self.docker.events.count("restore"), 1)
        self.assert_old_healthy_and_restored()
        self.assertEqual(OLD_REF, deploy.image_reference(OLD_ID, "rollback"))
        self.assertIn("CLIPROXY_MODE: managed", self.rollback_file.read_text())
        self.assertIn("STORAGE_ENABLED", self.rollback_file.read_text())

    def test_candidate_boot_failure_restores_snapshot_before_old_start(self):
        self.docker.fail = "candidate-boot"
        with self.assertRaises(deploy.DeploymentError):
            self.run_transaction()
        self.assertLess(self.docker.events.index("restore"), self.docker.events.index("start-old-stage"))
        self.assert_old_healthy_and_restored()

    def test_unexpected_storage_bind_fails_closed_after_commit(self):
        self.docker.bad_loopback = True
        with self.assertRaisesRegex(deploy.DeploymentError, "loopback"):
            self.run_transaction()
        self.assertIn("commit-before-storage", self.docker.events)
        self.assertNotIn("restore", self.docker.events)
        self.assertTrue((self.control / "maintenance").exists())
        self.assertTrue(deploy.PENDING.exists())
        journal = deploy.PENDING.read_text()
        self.assertNotIn("synthetic-secret", journal)
        self.assertNotIn("retired-secret", journal)

    def test_snapshot_failure_never_runs_migration_or_restores_incomplete_backup(self):
        self.docker.fail = "snapshot"
        with self.assertRaisesRegex(deploy.DeploymentError, "snapshot failure"):
            self.run_transaction()
        self.assertNotIn("import-preview", self.docker.events)
        self.assertNotIn("restore", self.docker.events)
        self.assert_old_healthy_and_restored()

    def test_failed_rollback_keeps_maintenance_and_journal(self):
        self.docker.fail = "candidate-boot"
        original_compose = self.docker.compose

        def compose_with_old_failure(image, *args, **kwargs):
            if args[:1] == ("up",) and (kwargs.get("file") == deploy.ROLLBACK_COMPOSE
                                         or Path(kwargs.get("file")).name == "compose.rollback.stage.yml"):
                self.docker.events.append("start-old-failed")
                raise deploy.DeploymentError("synthetic old boot failure")
            return original_compose(image, *args, **kwargs)

        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(self.docker, "compose", side_effect=compose_with_old_failure):
            with self.assertRaisesRegex(deploy.DeploymentError, "candidate boot"):
                self.run_transaction()
        self.assertIn("recovery=intervention-required", output.getvalue())
        self.assertTrue((self.control / "maintenance").exists())
        self.assertTrue(deploy.PENDING.exists())

    def test_volume_restore_failure_keeps_gate_and_recovery_journal(self):
        self.docker.fail = "apply"

        def fail_restore(*args, **kwargs):
            self.docker.events.append("restore")
            raise deploy.DeploymentError("synthetic restore failure")

        with patch.object(self.docker, "restore", side_effect=fail_restore):
            with self.assertRaisesRegex(deploy.DeploymentError, "apply failed"):
                self.run_transaction()
        self.assertTrue((self.control / "maintenance").exists())
        self.assertTrue(deploy.PENDING.exists())
        self.assertNotIn("start-old-final", self.docker.events)
        self.assertEqual(json.loads(deploy.PENDING.read_text())["phase"], "rollback-failed")

    def test_public_probe_failure_after_open_never_restores_stale_snapshot(self):
        self.docker.ingress.fail_open_once = True
        with self.assertRaises(deploy.DeploymentError):
            self.run_transaction()
        self.assertNotIn("restore", self.docker.events)
        self.assertEqual(self.docker.active, "candidate-id")
        self.assertEqual(self.docker.volume_data["database"], "candidate startup wrote database")
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())

    def test_pending_cleanup_failure_after_completion_rechecks_health_before_cleanup(self):
        with patch.object(deploy, "clear_pending", side_effect=OSError("synthetic unlink/fsync failure")):
            self.assertEqual(self.run_transaction(), 0)
            self.assertTrue(deploy.PENDING.exists())
            self.assertTrue((self.transactions / TRANSACTION).exists())
            self.assertFalse((self.control / "maintenance").exists())
            self.assertEqual(json.loads(deploy.ACTIVE.read_text())["status"], "complete")
            calls = list(self.docker.events)
            deploy.enable_maintenance(TRANSACTION)
            self.docker.containers["candidate-id"]["State"]["Health"]["Status"] = "unhealthy"
            deploy.recover(self.docker)
            self.assertNotEqual(self.docker.events, calls)
            self.assertIn("start-candidate-final", self.docker.events[len(calls):])
            self.assertFalse((self.control / "maintenance").exists())
            self.assertTrue(deploy.PENDING.exists())
            self.assertTrue((self.transactions / TRANSACTION).exists())

    def leave_completed_journal(self):
        with patch.object(deploy, "clear_pending", side_effect=OSError("synthetic pending cleanup failure")), \
                patch.object(deploy, "cleanup_transaction_directory", side_effect=OSError("synthetic artifact cleanup failure")):
            self.assertEqual(self.run_transaction(), 0)
        self.assertTrue(deploy.PENDING.exists())
        record = deploy.read_pending()
        return deploy.Transaction(
            self.docker, record,
            Path(record["candidate_stage_compose"]),
            Path(record["candidate_compose"]),
            Path(record["rollback_stage_compose"]),
            self.docker.ingress,
        )

    def write_recovery_journal(self, phase, label):
        transaction_dir = self.transactions / TRANSACTION
        transaction_dir.mkdir(mode=0o700)
        candidate_compose = transaction_dir / "compose.final.yml"
        candidate_stage = transaction_dir / "compose.stage.yml"
        rollback_stage = transaction_dir / "compose.rollback.stage.yml"
        for path in (candidate_compose, candidate_stage, rollback_stage):
            path.write_text("services: {}\n")
            path.chmod(0o600)
        runtime = transaction_dir / "runtime.env"
        runtime.write_text("PUBLIC_SETTING=value\n")
        runtime.chmod(0o600)
        backup_dir = self.backups / f"recovery-{label}-{TRANSACTION}"
        backup_dir.mkdir(mode=0o700)
        backup = backup_dir / "volume.tar"
        fake_snapshot(backup)
        record = {
            "transaction": TRANSACTION, "source_image": IMAGE, "target_ref": TARGET_REF,
            "target_image_id": TARGET_ID, "old_ref": OLD_REF, "old_image_id": OLD_ID,
            "backup": str(backup), "phase": phase, "candidate_compose": str(candidate_compose),
            "candidate_stage_compose": str(candidate_stage),
            "rollback_stage_compose": str(rollback_stage),
            "data_may_change": True, "snapshot_complete": True, "gate_verified": True,
            "snapshot": deploy.verify_snapshot(backup),
            "previous_compose": str(deploy.ROLLBACK_COMPOSE),
            "previous_compose_sha256": deploy.private_file_digest(deploy.ROLLBACK_COMPOSE),
            "candidate_source_compose_sha256": deploy.private_file_digest(deploy.COMPOSE),
            "candidate_compose_sha256": deploy.private_file_digest(candidate_compose),
            "candidate_stage_compose_sha256": deploy.private_file_digest(candidate_stage),
            "rollback_stage_compose_sha256": deploy.private_file_digest(rollback_stage),
            "candidate_runtime_sha256": deploy.private_file_digest(runtime),
            "maintenance_before": False,
            "proxy_image_ref": deploy.PROXY_IMAGE,
            "rollback_compose_hashes": {
                "application": ROLLBACK_APP_HASH,
                "stage": ROLLBACK_STAGE_HASH,
                "proxy": PROXY_HASH,
            },
        }
        deploy.save_json(deploy.PENDING, record)
        deploy.enable_maintenance(TRANSACTION)
        return record

    def test_commit_recovery_reaffirms_phase_after_replace_before_any_storage_publication(self):
        real_sync = deploy.sync_directory
        for phase, marker, final_event in (
                ("storage-opening", "committed", "start-candidate-final"),
                ("rollback-storage-opening", "rollback-committed", "start-old-final")):
            with self.subTest(phase=phase):
                if phase == "rollback-storage-opening":
                    self.docker = FakeDocker(self.control)
                record = self.write_recovery_journal("candidate-ready", phase)
                tx = deploy.Transaction(
                    self.docker, record,
                    Path(record["candidate_stage_compose"]),
                    Path(record["candidate_compose"]),
                    Path(record["rollback_stage_compose"]),
                    self.docker.ingress,
                )

                def fail_state_fsync(path):
                    if path == self.state:
                        raise KeyboardInterrupt("synthetic crash after journal replace")
                    return real_sync(path)

                with patch.object(deploy, "sync_directory", side_effect=fail_state_fsync):
                    with self.assertRaisesRegex(KeyboardInterrupt, "journal replace"):
                        tx.save(phase=phase, traffic_may_have_opened=True)
                self.assertEqual(tx.record["phase"], "candidate-ready")
                self.assertEqual(json.loads(deploy.PENDING.read_text())["phase"], phase)

                # A crash during recovery's commit re-affirmation must not start
                # either candidate or old Storage with an uncertain journal.
                for _ in range(2):
                    with patch.object(deploy, "sync_directory", side_effect=fail_state_fsync):
                        with self.assertRaisesRegex(KeyboardInterrupt, "journal replace"):
                            deploy.recover(self.docker)
                    self.assertEqual(json.loads(deploy.PENDING.read_text())["phase"], marker)
                    self.assertNotIn(final_event, self.docker.events)
                    self.assertNotIn("restore", self.docker.events)

                deploy.recover(self.docker)
                self.assertIn(final_event, self.docker.events)
                self.assertNotIn("restore", self.docker.events)
                self.assertFalse((self.control / "maintenance").exists())
                self.assertFalse(deploy.PENDING.exists())

    def test_active_marker_postreplace_fsync_failure_keeps_recovery_authoritative(self):
        tx = self.leave_completed_journal()

        def fail_after_active_replace(path):
            if path == self.state:
                raise KeyboardInterrupt("synthetic crash after ACTIVE replace")

        with patch.object(deploy, "sync_directory", side_effect=fail_after_active_replace):
            with self.assertRaisesRegex(KeyboardInterrupt, "ACTIVE replace"):
                tx.complete(TARGET_REF, TARGET_ID, source=IMAGE, migration="codex-credentials")
        self.assertFalse(tx.completed)
        self.assertEqual(json.loads(deploy.ACTIVE.read_text())["status"], "complete")
        self.assertTrue(deploy.PENDING.exists())

        deploy.enable_maintenance(TRANSACTION)
        self.docker.fail = "candidate-boot"
        with self.assertRaisesRegex(deploy.DeploymentError, "Committed candidate recovery failed"):
            deploy.recover(self.docker)
        self.assertTrue((self.control / "maintenance").exists())
        self.assertTrue(deploy.PENDING.exists())
        self.assertEqual(json.loads(deploy.PENDING.read_text())["phase"], "committed-recovery-needed")
        self.assertNotIn("restore", self.docker.events)

        self.docker.fail = None
        deploy.recover(self.docker)
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())
        self.assertNotIn("restore", self.docker.events)

    def test_interruption_after_cleanup_does_not_trigger_traffic_recovery(self):
        cleanup = deploy.cleanup_completed_transaction

        def cleanup_then_interrupt(record):
            self.assertTrue(cleanup(record))
            raise KeyboardInterrupt("synthetic interruption after completion cleanup")

        with patch.object(deploy, "cleanup_completed_transaction", side_effect=cleanup_then_interrupt):
            self.assertEqual(self.run_transaction(), 0)
        self.assertEqual(json.loads(deploy.ACTIVE.read_text())["status"], "complete")
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())
        self.assertFalse((self.transactions / TRANSACTION).exists())
        self.assertEqual(self.docker.events.count("start-candidate-final"), 1)
        self.assertEqual(self.docker.events.count("public-health"), 2)
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())

    def test_transaction_artifact_cleanup_failure_after_completion_is_best_effort(self):
        with patch.object(deploy, "cleanup_transaction_directory", side_effect=OSError("synthetic cleanup failure")):
            self.assertEqual(self.run_transaction(), 0)
        self.assertFalse(deploy.PENDING.exists())
        self.assertFalse((self.control / "maintenance").exists())
        self.assertEqual(json.loads(deploy.ACTIVE.read_text())["status"], "complete")
        self.assertTrue((self.transactions / TRANSACTION).exists())
        self.assertEqual(self.docker.events.count("start-candidate-final"), 1)

    def test_old_deployment_open_retry_never_reapplies_snapshot(self):
        self.docker.fail = "candidate-boot"
        self.docker.ingress.fail_open_once = True
        with self.assertRaises(deploy.DeploymentError):
            self.run_transaction()
        self.assertEqual(self.docker.events.count("restore"), 1)
        self.assertLess(self.docker.events.index("commit-before-old-storage"), self.docker.events.index("start-old-final"))
        self.assert_old_healthy_and_restored()

    def test_candidate_compose_drops_engine_and_keeps_storage_settings(self):
        source_root = Path(deploy.__file__).parent
        candidate = (source_root / "compose.yml").read_text()
        self.assertNotIn("CLIPROXY_MODE", candidate)
        self.assertIn("STORAGE_ENABLED", candidate)
        self.assertIn('"9000"', candidate)
        self.assertIn("dependencies?.storage", candidate)
        rollback = (source_root / "compose.rollback.yml").read_text()
        self.assertIn("CLIPROXY_MODE: managed", rollback)
        self.assertIn("STORAGE_ENABLED", rollback)
        self.assertIn('"9000"', rollback)
        self.assertIn("dependencies?.cliproxy", rollback)
        self.assertNotIn("dependencies?.storage", rollback)

    def test_effective_old_compose_mismatch_aborts_before_downtime(self):
        self.docker.hash_mismatch = True
        with self.assertRaisesRegex(deploy.DeploymentError, "do not exactly match"):
            self.run_transaction()
        self.assertTrue(self.docker.containers["old-id"]["State"]["Running"])
        self.assertNotIn("stop-old", self.docker.events)
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())

    def test_storage_commit_journal_failure_does_not_publish_or_advance_memory(self):
        for fail_after_replace in (False, True):
            with self.subTest(fail_after_replace=fail_after_replace):
                original = deploy.save_json
                failed = False

                def fail_storage_commit(path, value):
                    nonlocal failed
                    if path == deploy.PENDING and value.get("phase") == "storage-opening" and not failed:
                        failed = True
                        if fail_after_replace:
                            original(path, value)
                        raise OSError("synthetic journal fsync failure")
                    return original(path, value)

                with patch.object(deploy, "save_json", side_effect=fail_storage_commit):
                    with self.assertRaisesRegex(OSError, "journal fsync"):
                        self.run_transaction(transaction=("2" if fail_after_replace else "3") * 32)
                self.assertTrue(failed)
                self.assertNotIn("start-candidate-final", self.docker.events)
                self.assertIn("restore", self.docker.events)
                self.assert_old_healthy_and_restored()
                if not fail_after_replace:
                    self.docker = FakeDocker(self.control)

    def test_preopen_recovery_restores_snapshot_and_matching_old_deployment(self):
        transaction_dir = self.transactions / TRANSACTION
        transaction_dir.mkdir(mode=0o700)
        candidate_compose = transaction_dir / "compose.final.yml"
        candidate_compose.write_text("services: {}\n")
        candidate_compose.chmod(0o600)
        candidate_stage = transaction_dir / "compose.stage.yml"
        candidate_stage.write_text("services: {}\n")
        candidate_stage.chmod(0o600)
        rollback_stage = transaction_dir / "compose.rollback.stage.yml"
        rollback_stage.write_text("services: {}\n")
        rollback_stage.chmod(0o600)
        (transaction_dir / "runtime.env").write_text("PUBLIC_SETTING=value\n")
        (transaction_dir / "runtime.env").chmod(0o600)
        backup_dir = self.backups / f"recovery-{TRANSACTION}"
        backup_dir.mkdir(mode=0o700)
        backup = backup_dir / "volume.tar"
        fake_snapshot(backup)
        self.docker.snapshot_data = {"database": "before", "storage-key": "synthetic-storage-key", "other": "preserved"}
        self.docker.containers["old-id"]["State"]["Running"] = False
        self.docker.containers["candidate-id"] = app_container("candidate-id", TARGET_ID)
        self.docker.active = "candidate-id"
        self.docker.volume_data["database"] = "after migration"
        record = {
            "transaction": TRANSACTION, "source_image": IMAGE, "target_ref": TARGET_REF,
            "target_image_id": TARGET_ID, "old_ref": OLD_REF, "old_image_id": OLD_ID,
            "backup": str(backup), "phase": "candidate-boot-intent", "candidate_compose": str(candidate_compose),
            "candidate_stage_compose": str(candidate_stage),
            "rollback_stage_compose": str(rollback_stage),
            "data_may_change": True, "snapshot_complete": True, "gate_verified": True,
            "snapshot": deploy.verify_snapshot(backup),
            "previous_compose": str(deploy.ROLLBACK_COMPOSE),
            "previous_compose_sha256": deploy.private_file_digest(deploy.ROLLBACK_COMPOSE),
            "candidate_source_compose_sha256": deploy.private_file_digest(deploy.COMPOSE),
            "candidate_compose_sha256": deploy.private_file_digest(candidate_compose),
            "candidate_stage_compose_sha256": deploy.private_file_digest(candidate_stage),
            "rollback_stage_compose_sha256": deploy.private_file_digest(rollback_stage),
            "candidate_runtime_sha256": deploy.private_file_digest(transaction_dir / "runtime.env"),
            "maintenance_before": False,
            "proxy_image_ref": deploy.PROXY_IMAGE,
            "rollback_compose_hashes": {
                "application": ROLLBACK_APP_HASH,
                "stage": ROLLBACK_STAGE_HASH,
                "proxy": PROXY_HASH,
            },
        }
        deploy.save_json(deploy.PENDING, record)
        deploy.enable_maintenance(TRANSACTION)
        deploy.recover(self.docker)
        self.assertIn("restore", self.docker.events)
        self.assert_old_healthy_and_restored()

    def test_postopen_recovery_never_restores_pre_migration_data(self):
        for phase in ("opening", "opened"):
            with self.subTest(phase=phase):
                if deploy.PENDING.exists():
                    deploy.PENDING.unlink()
                self.docker.events.clear()
                transaction_dir = self.transactions / TRANSACTION
                transaction_dir.mkdir(mode=0o700, exist_ok=True)
                candidate_compose = transaction_dir / "compose.final.yml"
                candidate_compose.write_text("services: {}\n")
                candidate_compose.chmod(0o600)
                candidate_stage = transaction_dir / "compose.stage.yml"
                candidate_stage.write_text("services: {}\n")
                candidate_stage.chmod(0o600)
                rollback_stage = transaction_dir / "compose.rollback.stage.yml"
                rollback_stage.write_text("services: {}\n")
                rollback_stage.chmod(0o600)
                (transaction_dir / "runtime.env").write_text("PUBLIC_SETTING=value\n")
                (transaction_dir / "runtime.env").chmod(0o600)
                backup_dir = self.backups / f"recovery-{phase}-{TRANSACTION}"
                backup_dir.mkdir(mode=0o700)
                backup = backup_dir / "volume.tar"
                fake_snapshot(backup)
                self.docker.snapshot_data = {"database": "before", "storage-key": "synthetic-storage-key", "other": "preserved"}
                self.docker.containers["candidate-id"] = app_container("candidate-id", TARGET_ID)
                self.docker.active = "candidate-id"
                self.docker.volume_data["database"] = "after public traffic"
                record = {
                    "transaction": TRANSACTION, "source_image": IMAGE, "target_ref": TARGET_REF,
                    "target_image_id": TARGET_ID, "old_ref": OLD_REF, "old_image_id": OLD_ID,
                    "backup": str(backup), "phase": phase, "candidate_compose": str(candidate_compose),
                    "candidate_stage_compose": str(candidate_stage),
                    "rollback_stage_compose": str(rollback_stage),
                    "data_may_change": True, "snapshot_complete": True, "gate_verified": True,
                    "snapshot": deploy.verify_snapshot(backup),
                    "previous_compose": str(deploy.ROLLBACK_COMPOSE),
                    "previous_compose_sha256": deploy.private_file_digest(deploy.ROLLBACK_COMPOSE),
                    "candidate_source_compose_sha256": deploy.private_file_digest(deploy.COMPOSE),
                    "candidate_compose_sha256": deploy.private_file_digest(candidate_compose),
                    "candidate_stage_compose_sha256": deploy.private_file_digest(candidate_stage),
                    "rollback_stage_compose_sha256": deploy.private_file_digest(rollback_stage),
                    "candidate_runtime_sha256": deploy.private_file_digest(transaction_dir / "runtime.env"),
                    "maintenance_before": False,
                    "proxy_image_ref": deploy.PROXY_IMAGE,
                    "rollback_compose_hashes": {
                        "application": ROLLBACK_APP_HASH,
                        "stage": ROLLBACK_STAGE_HASH,
                        "proxy": PROXY_HASH,
                    },
                }
                deploy.save_json(deploy.PENDING, record)
                deploy.recover(self.docker)
                self.assertNotIn("restore", self.docker.events)
                self.assertNotEqual(self.docker.volume_data["database"], "before")
                self.assertEqual(self.docker.active, "candidate-id")
                self.assertFalse((self.control / "maintenance").exists())
                self.assertFalse(deploy.PENDING.exists())

    def test_rollback_storage_recovery_never_restores_after_local_clients_may_write(self):
        transaction_dir = self.transactions / TRANSACTION
        transaction_dir.mkdir(mode=0o700)
        candidate_compose = transaction_dir / "compose.final.yml"
        candidate_compose.write_text("services: {}\n")
        candidate_compose.chmod(0o600)
        candidate_stage = transaction_dir / "compose.stage.yml"
        candidate_stage.write_text("services: {}\n")
        candidate_stage.chmod(0o600)
        rollback_stage = transaction_dir / "compose.rollback.stage.yml"
        rollback_stage.write_text("services: {}\n")
        rollback_stage.chmod(0o600)
        (transaction_dir / "runtime.env").write_text("PUBLIC_SETTING=value\n")
        (transaction_dir / "runtime.env").chmod(0o600)
        backup_dir = self.backups / f"rollback-commit-{TRANSACTION}"
        backup_dir.mkdir(mode=0o700)
        backup = backup_dir / "volume.tar"
        fake_snapshot(backup)
        self.docker.snapshot_data = {"database": "before", "storage-key": "synthetic-storage-key", "other": "preserved"}
        self.docker.containers["old-id"] = app_container("old-id", OLD_ID, exposed=True)
        self.docker.active = "old-id"
        self.docker.storage_state["open"] = True
        self.docker.volume_data["database"] = "after local Storage write"
        record = {
            "transaction": TRANSACTION, "source_image": IMAGE, "target_ref": TARGET_REF,
            "target_image_id": TARGET_ID, "old_ref": OLD_REF, "old_image_id": OLD_ID,
            "backup": str(backup), "phase": "rollback-storage-opening",
            "candidate_compose": str(candidate_compose), "candidate_stage_compose": str(candidate_stage),
            "rollback_stage_compose": str(rollback_stage), "data_may_change": True,
            "snapshot_complete": True, "gate_verified": True,
            "snapshot": deploy.verify_snapshot(backup),
            "previous_compose": str(deploy.ROLLBACK_COMPOSE),
            "previous_compose_sha256": deploy.private_file_digest(deploy.ROLLBACK_COMPOSE),
            "candidate_source_compose_sha256": deploy.private_file_digest(deploy.COMPOSE),
            "candidate_compose_sha256": deploy.private_file_digest(candidate_compose),
            "candidate_stage_compose_sha256": deploy.private_file_digest(candidate_stage),
            "rollback_stage_compose_sha256": deploy.private_file_digest(rollback_stage),
            "candidate_runtime_sha256": deploy.private_file_digest(transaction_dir / "runtime.env"),
            "maintenance_before": False,
            "proxy_image_ref": deploy.PROXY_IMAGE,
            "rollback_compose_hashes": {
                "application": ROLLBACK_APP_HASH,
                "stage": ROLLBACK_STAGE_HASH,
                "proxy": PROXY_HASH,
            },
        }
        deploy.save_json(deploy.PENDING, record)
        deploy.enable_maintenance(TRANSACTION)
        deploy.recover(self.docker)
        self.assertNotIn("restore", self.docker.events)
        self.assertEqual(self.docker.volume_data["database"], "after local Storage write")
        self.assertEqual(self.docker.active, "old-id")
        self.assertFalse((self.control / "maintenance").exists())
        self.assertFalse(deploy.PENDING.exists())

    def test_journal_and_output_never_include_runtime_credentials(self):
        self.docker.fail = "apply"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(deploy.DeploymentError):
                self.run_transaction()
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("retired-secret", output.getvalue())
        self.assertNotIn("synthetic-auth-token", output.getvalue())
        backup_journals = list(self.state.glob("pending.json"))
        self.assertEqual(backup_journals, [])

    def test_importer_command_is_networkless_and_uses_exact_bundled_paths(self):
        runner = deploy.Docker()
        report = {"applied": False, "required": True, "accounts": 1, "resetRecords": 2}
        with patch.object(runner, "run", return_value=json.dumps(report)) as run:
            result = runner.importer_preview(
                TARGET_REF, self.backups / "volume.tar", self.backups, TRANSACTION, "preview-helper",
            )
        args = run.call_args.args[0]
        self.assertEqual(result, report)
        self.assertEqual(args[args.index("--network") + 1], "none")
        mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]
        self.assertEqual(mounts[0], "type=volume,dst=/data,volume-nocopy")
        self.assertNotIn(deploy.VOLUME, mounts[0])
        self.assertIn("dst=/snapshot.tar,readonly", mounts[1])
        self.assertIn("dst=/backup,readonly", mounts[2])
        command = args[args.index("-ec") + 1]
        self.assertLess(command.index("-xpf /snapshot.tar"), command.index("-dpf /snapshot.tar"))
        self.assertLess(command.index("-dpf /snapshot.tar"), command.index("sync -f /data"))
        self.assertLess(command.index("sync -f /data"), command.index("bun run --no-env-file"))
        self.assertIn("--sqlite /data/rawroute.sqlite", command)
        self.assertIn("--auth-dir /data/cliproxy/auth", command)
        self.assertIn("--config /data/cliproxy/config.yaml", command)
        self.assertIn("migrations/codex-credentials.js", command)
        self.assertEqual(args[args.index("--user") + 1], "0:0")

        apply_report = {"applied": True, "required": False, "accounts": 1, "resetRecords": 2}
        with patch.object(runner, "run", return_value=json.dumps(apply_report)) as apply_run:
            result = runner.importer(TARGET_REF, deploy.VOLUME, self.backups, TRANSACTION, "apply-helper", apply=True)
        self.assertEqual(result, apply_report)
        apply_args = apply_run.call_args.args[0]
        apply_mounts = [apply_args[index + 1] for index, value in enumerate(apply_args) if value == "--mount"]
        self.assertIn(f"src={deploy.VOLUME},dst=/data", apply_mounts[0])
        self.assertNotIn("readonly", apply_mounts[0])
        self.assertNotIn("readonly", apply_mounts[1])
        self.assertIn("--writers-stopped", apply_args)
        self.assertEqual(apply_args[apply_args.index("--backup-dir") + 1], "/backup/importer")
        with self.assertRaisesRegex(deploy.DeploymentError, "disposable snapshot"):
            runner.importer(TARGET_REF, deploy.VOLUME, self.backups, TRANSACTION, "bad-preview", apply=False)

    def test_restore_command_syncs_volume_after_extract_and_compare(self):
        runner = deploy.Docker()
        calls = []
        with patch.object(runner, "run", side_effect=lambda args, **_kwargs: calls.append(args)):
            runner.restore(TARGET_ID, deploy.VOLUME, self.backups / "volume.tar", TRANSACTION, "restore")
        command = calls[0]
        self.assertEqual(command[command.index("--network") + 1], "none")
        script = command[command.index("-ec") + 1]
        self.assertIn("find -P /target", script)
        self.assertLess(script.index("find -P /target"), script.index("-xpf /backup/volume.tar"))
        self.assertLess(script.index("-xpf /backup/volume.tar"), script.index("-dpf /backup/volume.tar"))
        self.assertLess(script.index("-dpf /backup/volume.tar"), script.index("sync -f /target"))

    def test_interrupted_preview_cleanup_removes_its_anonymous_volume(self):
        runner = deploy.Docker()
        calls = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            if args[:2] == ["docker", "ps"]:
                return "helper-id"
            return None

        helper = {
            "Config": {"Labels": {deploy.HELPER_LABEL: TRANSACTION}},
            "State": {"Running": False},
        }
        with patch.object(runner, "run", side_effect=fake_run), patch.object(runner, "inspect", return_value=helper):
            runner.stop_helpers(TRANSACTION)
        self.assertEqual(calls[-1], ["docker", "rm", "-v", "helper-id"])

    def test_docker_command_errors_are_redacted(self):
        runner = deploy.Docker()
        output = io.StringIO()
        failure = subprocess.CompletedProcess(["docker"], 1, b"", b"secret-value")
        with patch.object(deploy.subprocess, "run", return_value=failure), contextlib.redirect_stderr(output):
            with self.assertRaises(deploy.DeploymentError) as raised:
                runner.run(["docker", "run", "--env", "secret-value"])
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertNotIn("secret-value", output.getvalue())

    def test_storage_must_be_loopback_and_site_probe_requires_explicit_hostname(self):
        good = app_container("candidate", TARGET_ID)
        deploy.check_storage_binding(good)
        bad = app_container("candidate", TARGET_ID, storage_ip="0.0.0.0")
        with self.assertRaisesRegex(deploy.DeploymentError, "loopback"):
            deploy.check_storage_binding(bad)
        self.assertEqual(deploy.site_host("rawroute.example.test"), "rawroute.example.test")
        self.assertEqual(deploy.site_target(":80"), ("http", "127.0.0.1"))
        for site in ("example.test,other.test", "https://example.test/path", "example.test:8080"):
            with self.assertRaises(deploy.DeploymentError):
                deploy.site_target(site)

    def test_http_tunnel_site_probes_maintenance_on_published_http_port(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"RawRoute is being updated. Please try again shortly.\n"
                self.send_response(503)
                self.send_header("Retry-After", "30")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        proxy = proxy_container(self.control)
        proxy["Config"]["Env"] = ["RAWROUTE_SITE=:80"]
        proxy["HostConfig"]["PortBindings"] = {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(server.server_port)}],
        }
        with patch.object(deploy, "Ingress", self.real_ingress):
            ingress = deploy.Ingress(self.docker, proxy)
        self.assertEqual(ingress.scheme, "http")
        self.assertEqual(ingress.port, server.server_port)
        ingress.assert_gated()


if __name__ == "__main__":
    unittest.main()
