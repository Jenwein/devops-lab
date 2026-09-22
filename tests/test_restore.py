import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import restore  # noqa: E402

MANIFEST = {
    "source": {"DEVOPS_LAB_ROOT": "/srv/source", "COMPOSE_PROJECT_NAME": "source", "BASE_DOMAIN": "source.test",
               "HTTPS_PORT": "443", "GITLAB_SSH_PORT": "2224", "BIND_ADDRESS": "0.0.0.0",
               "GITLAB_HOST": "gitlab.source.test", "JENKINS_HOST": "jenkins.source.test", "SONAR_HOST": "sonar.source.test",
               "GITLAB_URL": "https://gitlab.source.test", "JENKINS_URL": "https://jenkins.source.test",
               "SONAR_URL": "https://sonar.source.test"},
    "includes_images": False,
    "images": {"gitlab": {"id": "sha256:g", "digest": "d" * 64}},
}


class IdentityTests(unittest.TestCase):
    def test_defaults_come_from_manifest_and_flags_override(self) -> None:
        arguments = restore.parse_arguments(["--repo", "/r", "--backup", "/b", "--root", "/t"])
        self.assertEqual({"project": "source", "base_domain": "source.test", "https_port": "443",
                          "ssh_port": "2224", "bind_address": "0.0.0.0"}, restore.resolve_identity(MANIFEST, arguments))
        arguments = restore.parse_arguments(["--repo", "/r", "--backup", "/b", "--root", "/t",
                                             "--project", "clone", "--https-port", "9443", "--bind-address", "127.0.0.1"])
        identity = restore.resolve_identity(MANIFEST, arguments)
        self.assertEqual(("clone", "9443", "127.0.0.1", "source.test"),
                         (identity["project"], identity["https_port"], identity["bind_address"], identity["base_domain"]))

    def test_rehearsal_publishes_on_loopback_and_rejects_a_public_bind(self) -> None:
        # Compose appends `ports` across files rather than replacing them, so the overlay alone
        # cannot narrow a 0.0.0.0 publish from compose.yaml; the identity itself must be loopback.
        manifest = dict(MANIFEST, revision="a" * 40)
        arguments = restore.parse_arguments(["--repo", "/r", "--backup", "/b", "--root", "/t", "--rehearsal"])
        previous = os.umask(0o022)
        try:
            with mock.patch.object(restore, "load_versions"), \
                    mock.patch.object(restore, "verify_backup", return_value=manifest), \
                    mock.patch.object(restore, "validate_destination"), \
                    mock.patch.object(restore, "validate_tar"), \
                    mock.patch.object(restore, "run", side_effect=[b"a" * 40, b""]), \
                    mock.patch.object(restore.socket, "socket") as factory:
                probe = factory.return_value.__enter__.return_value
                probe.connect_ex.side_effect = RuntimeError("probe reached")
                with self.assertRaisesRegex(RuntimeError, "probe reached"):
                    restore.restore(arguments)
            self.assertEqual(("127.0.0.1", 443), probe.connect_ex.call_args.args[0])

            arguments = restore.parse_arguments(["--repo", "/r", "--backup", "/b", "--root", "/t",
                                                 "--rehearsal", "--bind-address", "0.0.0.0"])
            with mock.patch.object(restore, "load_versions"), \
                    mock.patch.object(restore, "verify_backup", return_value=manifest):
                with self.assertRaisesRegex(ValueError, "rehearsal must bind 127.0.0.1"):
                    restore.restore(arguments)
        finally:
            os.umask(previous)


class PortProbeTests(unittest.TestCase):
    def test_a_listening_port_is_reported_as_in_use(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, f"port {port} is already in use on 127.0.0.1"):
                restore.assert_port_free("127.0.0.1", port)

    def test_a_closed_port_is_accepted(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
        restore.assert_port_free("127.0.0.1", port)


class FenceTests(unittest.TestCase):
    def test_application_container_on_frontend_network_is_rejected(self) -> None:
        networks = [{"Name": "clone_edge", "Internal": True}, {"Name": "clone_sonar-db", "Internal": True},
                    {"Name": "clone_recovery-front", "Internal": False}]

        def container(service, attached):
            return {"Config": {"Labels": {"com.docker.compose.service": service}},
                    "NetworkSettings": {"Networks": dict.fromkeys(attached, {})},
                    "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": "127.0.0.1"}]}}}

        lab = mock.Mock()
        lab.env = {"COMPOSE_PROJECT_NAME": "clone", "HTTPS_PORT": "8443"}
        lab.dc.return_value = b"edge-id app-id"
        with mock.patch.object(restore, "run", side_effect=[
            json.dumps(networks).encode(),
            json.dumps([container("edge", ["clone_edge", "clone_recovery-front"])]).encode(),
            json.dumps([container("jenkins", ["clone_edge", "clone_recovery-front"])]).encode(),
        ]):
            with self.assertRaisesRegex(RuntimeError, "unexpected attached network"):
                restore.fence(lab, MANIFEST["source"])
        lab.exec.assert_not_called()

    def test_reachable_probe_is_rejected(self) -> None:
        networks = [{"Name": "clone_edge", "Internal": True}, {"Name": "clone_sonar-db", "Internal": True},
                    {"Name": "clone_recovery-front", "Internal": False}]

        def container(service, attached):
            return {"Config": {"Labels": {"com.docker.compose.service": service}},
                    "NetworkSettings": {"Networks": dict.fromkeys(attached, {})},
                    "HostConfig": {"PortBindings": {"8443/tcp": [{"HostIp": "127.0.0.1"}]}}}

        lab = mock.Mock()
        lab.env = {"COMPOSE_PROJECT_NAME": "clone", "HTTPS_PORT": "8443"}
        lab.dc.return_value = b"edge-id"
        lab.exec.return_value = b"REACHABLE\n"
        with mock.patch.object(restore, "run", side_effect=[
            json.dumps(networks).encode(), json.dumps([container("edge", ["clone_edge", "clone_recovery-front"])]).encode()]):
            with self.assertRaisesRegex(RuntimeError, "reach"):
                restore.fence(lab, MANIFEST["source"])


IMAGE_MANIFEST = dict(MANIFEST, includes_images=True, images={
    service: {"id": "sha256:" + str(index) * 64} for index, service in enumerate(restore.SERVICES)})


class OverlayTests(unittest.TestCase):
    def lab_in_temp_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / "config").mkdir()
        lab = mock.Mock()
        lab.root = root
        lab.env = {"COMPOSE_PROJECT_NAME": "clone", "HTTPS_PORT": "8443", "GITLAB_SSH_PORT": "2225"}
        return lab

    def test_rehearsal_overlay_isolates_networks_and_binds_loopback(self) -> None:
        lab = self.lab_in_temp_root()
        path = restore.restore_overlay(lab, MANIFEST, rehearsal=True)
        overlay = json.loads(path.read_text())
        self.assertTrue(overlay["networks"]["edge"]["internal"])
        self.assertTrue(overlay["networks"]["sonar-db"]["internal"])
        self.assertFalse(overlay["networks"]["recovery-front"]["internal"])
        self.assertEqual(["edge", "recovery-front"], overlay["services"]["edge"]["networks"])
        self.assertEqual(["127.0.0.1:8443:8443"], overlay["services"]["edge"]["ports"])
        self.assertEqual(["127.0.0.1:2225:22"], overlay["services"]["gitlab"]["ports"])
        for service in ("gitlab", "jenkins", "postgres", "sonarqube"):
            self.assertEqual("127.0.0.1", overlay["services"][service]["extra_hosts"]["gitlab.source.test"])
            self.assertNotIn("image", overlay["services"][service])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(path, lab.root / "config" / "recovery-compose.json")

    def test_a_plain_restore_writes_no_rehearsal_overlay(self) -> None:
        lab = self.lab_in_temp_root()
        self.assertIsNone(restore.restore_overlay(lab, MANIFEST, rehearsal=False))
        self.assertFalse((lab.root / "config" / "recovery-compose.json").exists())

    def test_image_pins_are_persisted_beside_runtime_env(self) -> None:
        """The pins outlive the restore so a later `scripts/lab up` neither pulls nor builds."""
        lab = self.lab_in_temp_root()
        path = restore.write_image_pins(lab, IMAGE_MANIFEST)
        self.assertEqual(lab.root / "config" / "images-pinned.json", path)
        pins = json.loads(path.read_text())
        self.assertEqual(set(restore.SERVICES), set(pins["services"]))
        for service in restore.SERVICES:
            self.assertEqual(IMAGE_MANIFEST["images"][service]["id"], pins["services"][service]["image"])
            self.assertEqual("never", pins["services"][service]["pull_policy"])
        self.assertNotIn("networks", pins)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)


class HelperImageTests(unittest.TestCase):
    def test_helper_image_comes_from_the_set_when_it_carries_images(self) -> None:
        lab = mock.Mock()
        lab.versions = {"jenkins": {"reference": "jenkins/jenkins:x@sha256:" + "0" * 64}}
        self.assertEqual(IMAGE_MANIFEST["images"]["jenkins"]["id"], restore.helper_image(lab, IMAGE_MANIFEST))
        self.assertEqual("jenkins/jenkins:x@sha256:" + "0" * 64, restore.helper_image(lab, MANIFEST))


class ReadinessAndTeardownTests(unittest.TestCase):
    def test_readiness_retries_transient_unavailability(self) -> None:
        lab = mock.Mock()
        lab.exec.side_effect = [RuntimeError("initializing"), b""]
        with mock.patch.object(restore.time, "sleep") as sleep:
            restore.wait_gitlab_ready(lab, timeout=10)
        self.assertEqual(2, lab.exec.call_count)
        sleep.assert_called_once()

    def test_teardown_reports_original_and_cleanup_without_output(self) -> None:
        lab = mock.Mock()
        lab.dc.side_effect = RuntimeError("sensitive compose output")
        original = TimeoutError("sensitive original output")
        with self.assertRaises(restore.CleanupError) as raised:
            restore.teardown(lab, original=original)
        lab.dc.assert_called_once_with("down", "--volumes", timeout=300)
        self.assertIs(original, raised.exception.original)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertIn("rehearsal teardown: TimeoutError", str(raised.exception))

    def test_place_gitlab_config_uses_the_given_root_helper_image(self) -> None:
        lab = mock.Mock()
        lab.root = pathlib.Path("/srv/clone")
        with mock.patch.object(restore, "run") as run:
            restore.place_gitlab_config(lab, pathlib.Path("/b/gitlab/config.tar"), "sha256:" + "a" * 64)
        command = run.call_args.args[0]
        self.assertEqual(["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh"], command[:7])
        self.assertIn("/srv/clone/data/gitlab/config:/target", " ".join(command))
        self.assertIn("sha256:" + "a" * 64, command)
        self.assertNotIn("jenkins/jenkins", " ".join(command))
        self.assertIn("--numeric-owner", " ".join(command))


if __name__ == "__main__":
    unittest.main()
