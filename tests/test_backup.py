import contextlib
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests"))
import backup  # noqa: E402
import compose  # noqa: E402
from fakes import FakeTransport  # noqa: E402


def lab_mock() -> mock.Mock:
    lab = mock.Mock()
    lab.versions = compose.load_versions(REPOSITORY)
    lab.env = {"GITLAB_URL": "https://gitlab.s.test", "GITLAB_HOST": "gitlab.s.test",
               "JENKINS_URL": "https://jenkins.s.test", "JENKINS_HOST": "jenkins.s.test",
               "SONAR_URL": "https://sonar.s.test", "SONAR_HOST": "sonar.s.test"}
    return lab


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_records_health_without_project_queries(self) -> None:
        lab = lab_mock()

        def execute(service, *arguments, **kwargs):
            command = " ".join(str(a) for a in arguments)
            if service == "gitlab" and "healthcheck" in command:
                return b"GitLab healthy\n"
            if service == "gitlab" and "doctor:secrets" in command:
                return b"No failures\n"
            if service == "gitlab":
                return b"Checking GitLab ... Finished\n"
            raise AssertionError(service)

        lab.exec.side_effect = execute
        lab.sonar.return_value = FakeTransport({("GET", "/api/ce/activity"): (200, {"tasks": []}),
                                                ("GET", "/api/server/version"): (200, b"26.9.0.129388\n")})
        lab.groovy.return_value = '{"quiet":false,"executors":0,"busyExecutors":0,"queueLength":0}'
        result = backup.checkpoint(lab)
        self.assertEqual(lab.versions["gitlab"]["tag"], result["versions"]["gitlab"])
        self.assertEqual({"health": "passed", "check": "passed", "doctor": "passed"}, result["gitlab"])
        self.assertEqual(0, result["jenkins"]["executors"])
        self.assertTrue(result["sonarqube"]["ce_idle"])
        self.assertEqual("26.9.0.129388", result["sonarqube"]["version"])
        calls = " ".join(" ".join(str(a) for a in call.args) for call in lab.exec.call_args_list)
        self.assertNotIn("project", calls)

    def test_busy_sonar_blocks_checkpoint(self) -> None:
        lab = lab_mock()
        lab.exec.return_value = b"ok"
        lab.sonar.return_value = FakeTransport({("GET", "/api/ce/activity"): (200, {"tasks": [{"id": "x"}]})})
        lab.groovy.return_value = '{"quiet":false,"executors":0,"busyExecutors":0,"queueLength":0}'
        with self.assertRaisesRegex(RuntimeError, "idle"):
            backup.checkpoint(lab)

    def test_sonar_checkpoint_is_authenticated_not_a_container_curl(self) -> None:
        """SonarQube forces authentication, so an in-container curl answers HTTP 401."""
        lab = lab_mock()
        lab.exec.return_value = b"ok"
        lab.sonar.return_value = FakeTransport({("GET", "/api/ce/activity"): (200, {"tasks": []}),
                                                ("GET", "/api/server/version"): (200, b"26.9.0.129388")})
        lab.groovy.return_value = '{"quiet":false,"executors":0,"busyExecutors":0,"queueLength":0}'
        backup.checkpoint(lab)
        commands = " ".join(" ".join(str(a) for a in call.args) for call in lab.exec.call_args_list)
        self.assertNotIn("curl", commands)
        lab.sonar.assert_called()


class PreconditionTests(unittest.TestCase):
    def test_quiesce_requires_idle_controller(self) -> None:
        lab = lab_mock()
        lab.groovy.return_value = "BUSY\n"
        with self.assertRaisesRegex(RuntimeError, "busy"):
            backup.quiesce_jenkins(lab)
        lab.groovy.return_value = "IDLE\n"
        backup.quiesce_jenkins(lab)

    def test_gitlab_safety_probe(self) -> None:
        lab = lab_mock()
        lab.exec.return_value = b"BUSY_OR_OBJECT_STORE\n"
        with self.assertRaisesRegex(RuntimeError, "GitLab"):
            backup.gitlab_safe_to_stop(lab)
        lab.exec.return_value = b"SAFE\n"
        backup.gitlab_safe_to_stop(lab)


class RecoverSourceTests(unittest.TestCase):
    def test_every_restart_is_attempted_and_summarised_without_output(self) -> None:
        lab = lab_mock()
        first = RuntimeError("private token from child output")
        lab.exec.side_effect = [first, b"", b""]
        original = ValueError("original secret response")
        with mock.patch.object(backup, "containers_ready", return_value=(True, "all containers healthy")):
            with self.assertRaises(backup.CleanupError) as raised:
                backup.recover_source(lab, gitlab_stopped=True, sonar_stopped=True, jenkins_stopped=True, original=original)
        self.assertEqual([mock.call("gitlab", "gitlab-ctl", "start", "sidekiq"), mock.call("gitlab", "gitlab-ctl", "start", "puma"),
                          mock.call("gitlab", "/opt/gitlab/bin/gitlab-healthcheck", "--fail", "--max-time", "10")],
                         lab.exec.call_args_list)
        self.assertEqual([mock.call("start", "sonarqube"), mock.call("start", "jenkins")], lab.dc.call_args_list)
        lab.groovy.assert_called_once()
        self.assertIs(original, raised.exception.original)
        self.assertEqual([("start GitLab Sidekiq", first)], raised.exception.cleanup_errors)
        self.assertIn("backup operation: ValueError", str(raised.exception))
        self.assertIn("start GitLab Sidekiq: RuntimeError", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private token", str(raised.exception))

    def test_gitlab_is_serving_again_before_the_backup_returns(self) -> None:
        """Otherwise `scripts/lab backup && scripts/lab status` reports 502 for a freshly restarted Puma."""
        lab = lab_mock()
        lab.groovy.return_value = "READY"
        lab.exec.side_effect = [b"", b"", RuntimeError("502"), RuntimeError("502"), b""]
        with mock.patch.object(backup.time, "sleep"), \
             mock.patch.object(backup, "containers_ready", side_effect=[(False, "starting"), (True, "ok")]) as ready:
            backup.recover_source(lab, gitlab_stopped=True, sonar_stopped=False, jenkins_stopped=False)
        self.assertEqual(2, ready.call_count)
        self.assertEqual(5, lab.exec.call_count)
        self.assertEqual(("gitlab", "/opt/gitlab/bin/gitlab-healthcheck", "--fail", "--max-time", "10"),
                         lab.exec.call_args.args)

    def test_quiet_mode_cancel_retries_until_jenkins_answers(self) -> None:
        lab = lab_mock()
        lab.groovy.side_effect = [RuntimeError("starting"), RuntimeError("starting"), "READY"]
        with mock.patch.object(backup.time, "sleep"):
            backup.recover_source(lab, gitlab_stopped=False, sonar_stopped=False, jenkins_stopped=False)
        self.assertEqual(3, lab.groovy.call_count)

    def test_clean_run_returns_without_error(self) -> None:
        lab = lab_mock()
        lab.groovy.return_value = "READY"
        backup.recover_source(lab, gitlab_stopped=False, sonar_stopped=False, jenkins_stopped=False)

    def test_all_restart_failures_are_collected_before_quiet_retry_exhaustion(self) -> None:
        lab = lab_mock()
        lab.exec.side_effect = RuntimeError("private restart output")
        lab.dc.side_effect = RuntimeError("private restart output")
        lab.groovy.side_effect = RuntimeError("private authenticated response")
        with mock.patch.object(backup.time, "sleep"), \
             mock.patch.object(backup, "containers_ready", return_value=(True, "ok")):
            with self.assertRaises(backup.CleanupError) as raised:
                backup.recover_source(lab, gitlab_stopped=True, sonar_stopped=True, jenkins_stopped=True)
        self.assertEqual(122, lab.exec.call_count)  # two restarts plus the exhausted readiness wait
        self.assertEqual(2, lab.dc.call_count)
        self.assertEqual(120, lab.groovy.call_count)
        self.assertEqual(6, len(raised.exception.cleanup_errors))
        self.assertIn("wait for GitLab", str(raised.exception))
        self.assertIn("cancel Jenkins quiet mode", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))


class ImageTests(unittest.TestCase):
    def test_running_images_join_container_image_ids_with_pinned_digests(self) -> None:
        lab = lab_mock()
        lab.dc.side_effect = lambda *a, **k: b"cid-" + a[-1].encode() + b"\n"
        inspected = {"Image": "sha256:abc", "Config": {"Image": "ref"}}
        with mock.patch.object(backup, "run", return_value=json.dumps([inspected]).encode()):
            images = backup.running_images(lab)
        self.assertEqual(set(compose.SERVICES), set(images))
        self.assertEqual("sha256:abc", images["gitlab"]["id"])
        self.assertEqual(lab.versions["gitlab"]["digest"], images["gitlab"]["digest"])
        self.assertNotIn("digest", images["edge"])
        self.assertNotIn("digest", images["jenkins"])


def _empty_tar() -> bytes:
    """Bytes of a valid, empty tar archive - enough for validate_tar to accept."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w"):
        pass
    return buffer.getvalue()


class CreateBackupTests(unittest.TestCase):
    """Pin the capture order: quiesce -> stop -> capture -> restart, with a real manifest on disk."""

    def test_create_backup_follows_the_quiesce_capture_restart_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "backups").mkdir()
            lab = lab_mock()
            lab.root = root
            lab.repo = root / "repo"

            ls_calls = {"count": 0}

            def fake_run(command, **kwargs):
                joined = " ".join(str(part) for part in command)
                if "rev-parse" in joined:
                    return b"a" * 40 + b"\n"
                if "status" in joined:
                    return b""
                if "inspect" in joined:
                    return b'[{"Image": "sha256:x"}]'
                for part in command:
                    if isinstance(part, pathlib.Path) and part.name == "code.bundle":
                        part.write_bytes(b"")
                return b""

            def fake_dc(*arguments, **kwargs):
                if arguments and arguments[0] == "ps":
                    return b"cid\n"
                if arguments and str(arguments[-1]).endswith("application.tar"):
                    pathlib.Path(arguments[-1]).write_bytes(b"")
                return b""

            def fake_exec(service, *arguments, **kwargs):
                output = kwargs.get("output")
                if output is not None:
                    output.write(_empty_tar())
                joined = " ".join(str(a) for a in arguments)
                if "ls" in joined and "gitlab_backup" in joined:
                    ls_calls["count"] += 1
                    if ls_calls["count"] == 1:
                        return b""
                    return b"/var/opt/gitlab/backups/1_gitlab_backup.tar\n"
                if "runner" in joined:
                    return b"SAFE"
                return b""

            def fake_archive_directory(source, target, excludes=()):
                pathlib.Path(target).write_bytes(b"")

            lab.dc.side_effect = fake_dc
            lab.exec.side_effect = fake_exec
            lab.groovy.side_effect = ["IDLE", "READY"]

            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(backup, "run", side_effect=fake_run), \
                 mock.patch.object(backup, "archive_directory", side_effect=fake_archive_directory), \
                 mock.patch.object(backup, "verify_backup"), \
                 mock.patch.object(backup, "installed_plugins", return_value={}), \
                 mock.patch.object(backup, "checkpoint", return_value={}), \
                 mock.patch.object(backup, "containers_ready", return_value=(True, "all containers healthy")):
                destination = backup.create_backup(lab, include_images=False)

            self.assertEqual(root / "backups", destination.parent)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(2, manifest["format"])
            self.assertFalse(manifest["includes_images"])
            self.assertEqual("1", manifest["gitlab_backup_id"])
            self.assertEqual(set(backup.REQUIRED), set(manifest["files"]))

            sequence = [(call[0], call[1]) for call in lab.mock_calls]

            def index_of(name, *args):
                for position, (call_name, call_args) in enumerate(sequence):
                    if call_name == name and call_args[:len(args)] == args:
                        return position
                raise AssertionError(f"no {name}{args} call recorded; calls were {sequence}")

            self.assertLess(index_of("dc", "stop", "jenkins"), index_of("dc", "stop", "sonarqube"))
            self.assertLess(index_of("exec", "gitlab", "gitlab-ctl", "stop", "puma"),
                            index_of("exec", "gitlab", "gitlab-backup", "create"))
            self.assertLess(index_of("exec", "gitlab", "gitlab-ctl", "stop", "sidekiq"),
                            index_of("exec", "gitlab", "gitlab-backup", "create"))
            self.assertLess(index_of("dc", "stop", "sonarqube"), index_of("exec", "postgres", "pg_dump"))

            # The archive is a second full copy of GitLab inside the container; drop it once
            # it is safely in the set, and before the capture continues.
            archive = "/var/opt/gitlab/backups/1_gitlab_backup.tar"
            removal = index_of("exec", "gitlab", "rm", "-f", archive)
            self.assertLess(index_of("dc", "cp", f"gitlab:{archive}"), removal)
            self.assertLess(removal, index_of("exec", "postgres", "pg_dump"))

            last_capture = index_of("exec", "postgres", "pg_restore", "--list")
            for restart in (("dc", "start", "sonarqube"), ("dc", "start", "jenkins"),
                            ("exec", "gitlab", "gitlab-ctl", "start", "sidekiq"),
                            ("exec", "gitlab", "gitlab-ctl", "start", "puma")):
                self.assertLess(last_capture, index_of(*restart))


if __name__ == "__main__":
    unittest.main()
