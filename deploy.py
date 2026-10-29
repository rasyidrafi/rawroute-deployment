#!/usr/bin/env python3
"""Single-writer deployment. Preview by default; mutations require --apply."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
VOLUME = "rawroute-production-data"
PROJECT = "rawroute-production"


def immutable(image):
    return bool(re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image) or
                re.fullmatch(r"[\w./:-]+:sha-[0-9a-f]{7,40}", image))


def run(args, *, env=None, capture=False):
    result = subprocess.run(args, cwd=ROOT, env=env, check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def compose(image, *args, capture=False):
    return run(["docker", "compose", "--project-name", PROJECT, "--env-file", str(ROOT / ".env"),
                "-f", str(ROOT / "compose.yml"), *args],
               env={**os.environ, "RAWROUTE_IMAGE": image}, capture=capture)


def containers_using_volume():
    return run(["docker", "ps", "-q", "--no-trunc", "--filter", f"volume={VOLUME}"], capture=True).split()


def assert_single_writer(allowed=None):
    unexpected = set(containers_using_volume()) - ({allowed} if allowed else set())
    if unexpected:
        raise RuntimeError("Another running container uses the production volume; stop it explicitly first.")


def ready(image):
    compose(image, "up", "-d", "--no-deps", "--pull", "never", "--wait",
            "--wait-timeout", "180", "rawroute")
    # Confirm Compose returned a healthy container before reopening traffic.
    container = compose(image, "ps", "-q", "rawroute", capture=True)
    health = run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container], capture=True)
    if health != "healthy":
        raise RuntimeError("Application is not ready; maintenance remains enabled.")


def apply(image, recover, local_image=False):
    pending = STATE / "pending.json"
    maintenance = STATE / "control" / "maintenance"
    if recover:
        if not pending.exists():
            raise RuntimeError("No interrupted deployment to recover.")
        record = json.loads(pending.read_text())
        previous = record.get("previous")
        if not previous:
            raise RuntimeError("No previous image. Resolve the failed initial install manually.")
        maintenance.touch()
        compose(previous, "stop", "rawroute")
        assert_single_writer()
        ready(previous)
        save(STATE / "active.json", {"image": previous})
        maintenance.unlink(missing_ok=True)
        pending.unlink()
        return
    if pending.exists():
        raise RuntimeError("Interrupted deployment found. Inspect .state/pending.json; recover before deploying again.")
    compose(image, "config", "--quiet")
    # Network/download time happens before maintenance and before stopping the writer.
    if local_image:
        run(["docker", "image", "inspect", image], capture=True)
        compose(image, "pull", "proxy")
    else:
        compose(image, "pull", "rawroute", "proxy")
    pinned = run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture=True)
    old = compose(image, "ps", "-aq", "rawroute", capture=True)
    if len(old.split()) > 1:
        raise RuntimeError("Expected at most one app container.")
    previous = None
    if old:
        previous = run(["docker", "inspect", "--format", "{{.Image}}", old], capture=True)
    assert_single_writer(old or None)
    run(["docker", "volume", "create", VOLUME])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = ROOT / "backups" / f"{timestamp}.tar"
    save(pending, {"previous": previous, "target": pinned, "backup": str(backup)})
    maintenance.touch()
    # Never recreate the proxy on an application update. Initial install starts it.
    proxy = compose(pinned, "ps", "-q", "proxy", capture=True)
    if not proxy:
        compose(pinned, "up", "-d", "--no-deps", "--pull", "never", "proxy")
    compose(pinned, "stop", "rawroute")
    assert_single_writer()
    # Snapshot the whole stopped volume: SQLite/WAL, secrets, engine state, storage key.
    run(["docker", "run", "--rm", "--network", "none", "--user", "0:0",
         "--mount", f"type=volume,src={VOLUME},dst=/source,readonly",
         "--mount", f"type=bind,src={backup.parent},dst=/backup",
         "--entrypoint", "/bin/sh", pinned, "-ec",
         f"umask 077; tar -C /source -cf /backup/{backup.name} ."])
    ready(pinned)
    save(STATE / "active.json", {"image": pinned, "source": image, "backup": str(backup)})
    maintenance.unlink(missing_ok=True)
    pending.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="Image with :sha-<commit> tag or @sha256:<digest>")
    parser.add_argument("--apply", action="store_true", help="Actually modify Docker and local deployment state")
    parser.add_argument("--schema-compatible", action="store_true",
                        help="Confirm no offline migration is needed and old/new images can use the same persisted data")
    parser.add_argument("--recover", action="store_true", help="Restart the previous image WITHOUT restoring data")
    parser.add_argument("--local-image", action="store_true", help="Use an already-built local application image")
    args = parser.parse_args(argv)
    if args.recover and args.image:
        parser.error("--recover does not accept an image")
    if not args.recover and (not args.image or not immutable(args.image)):
        parser.error("Provide an immutable :sha-<commit> or @sha256:<digest> image")
    if not args.apply:
        print("PREVIEW ONLY: no commands executed and no files created.")
        print("Recover previous image (no data restore)." if args.recover else
              f"Validate -> pre-pull {args.image} -> maintenance -> graceful stop -> full volume backup -> start -> readiness -> reopen traffic.")
        print("Requires --apply --schema-compatible. Offline migrations are deliberately not automated.")
        return 0
    if not args.schema_compatible:
        parser.error("--apply requires --schema-compatible; migration releases need a separate stopped-writer procedure")
    if not (ROOT / ".env").is_file():
        parser.error("Create .env from .env.example and set the public hostname first")
    STATE.mkdir(mode=0o700, exist_ok=True)
    (STATE / "control").mkdir(exist_ok=True)
    (ROOT / "backups").mkdir(mode=0o700, exist_ok=True)
    with (STATE / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment holds the lock.") from None
        apply(args.image, args.recover, local_image=args.local_image)
    print("Application ready; maintenance disabled. Check the public endpoint as well.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f"Deployment interrupted: {error}. If maintenance is enabled, it stays enabled. Inspect .state/pending.json.", file=sys.stderr)
        sys.exit(1)
