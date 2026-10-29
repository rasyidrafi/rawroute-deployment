import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy


class DeploymentTests(unittest.TestCase):
    def test_accepts_commit_or_digest(self):
        self.assertTrue(deploy.immutable("docker.io/rasyidrafi/rawroute:sha-e557de0"))
        self.assertTrue(deploy.immutable("registry.example:5000/app@sha256:" + "a" * 64))

    def test_rejects_mutable_or_invalid_images(self):
        for image in ["app:latest", "app:v1", "app", "app:sha-main", "app@sha256:abc", "app;echo bad"]:
            self.assertFalse(deploy.immutable(image))

    def test_preview_never_invokes_docker_or_writes(self):
        with patch.object(deploy, "run") as run, patch.object(deploy, "apply") as apply:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deploy.main(["app:sha-abcdef0"]), 0)
                self.assertEqual(deploy.main(["--recover"]), 0)
            run.assert_not_called()
            apply.assert_not_called()

    def test_apply_requires_compatibility_confirmation(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            deploy.main(["app:sha-abcdef0", "--apply"])

    def test_single_writer_guard(self):
        with patch.object(deploy, "containers_using_volume", return_value=["old"]):
            deploy.assert_single_writer("old")
            with self.assertRaises(RuntimeError):
                deploy.assert_single_writer()
        with patch.object(deploy, "containers_using_volume", return_value=["old", "other"]):
            with self.assertRaises(RuntimeError):
                deploy.assert_single_writer("old")

    def test_update_order_and_failure_keep_maintenance(self):
        for fail in [False, True]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".state"
                (state / "control").mkdir(parents=True)
                (root / "backups").mkdir()
                events = []

                def compose(image, *args, capture=False):
                    events.append(args[0])
                    if args == ("ps", "-aq", "rawroute"):
                        return "old"
                    if args == ("ps", "-q", "proxy"):
                        return "proxy"

                def run(args, **kwargs):
                    if args[:3] == ["docker", "image", "inspect"]:
                        return "sha256:new"
                    if args[:2] == ["docker", "inspect"]:
                        return "sha256:old"
                    if args[:2] == ["docker", "run"]:
                        events.append("backup")

                def ready(image):
                    events.append("ready")
                    self.assertTrue((state / "control" / "maintenance").exists())
                    if fail:
                        raise RuntimeError("unhealthy")

                with patch.object(deploy, "ROOT", root), patch.object(deploy, "STATE", state), \
                        patch.object(deploy, "compose", side_effect=compose), \
                        patch.object(deploy, "run", side_effect=run), \
                        patch.object(deploy, "ready", side_effect=ready), \
                        patch.object(deploy, "assert_single_writer"):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            deploy.apply("app:sha-abcdef0", False)
                    else:
                        deploy.apply("app:sha-abcdef0", False)
                self.assertLess(events.index("pull"), events.index("stop"))
                self.assertLess(events.index("stop"), events.index("backup"))
                self.assertLess(events.index("backup"), events.index("ready"))
                self.assertEqual((state / "pending.json").exists(), fail)
                self.assertEqual((state / "control" / "maintenance").exists(), fail)
                if fail:
                    self.assertEqual(json.loads((state / "pending.json").read_text())["previous"], "sha256:old")


if __name__ == "__main__":
    unittest.main()
