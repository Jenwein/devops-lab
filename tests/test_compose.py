import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import compose  # noqa: E402


class ComposeEnvironmentTests(unittest.TestCase):
    """Compose prefers the ambient environment over --env-file.

    A shell that exported DEVOPS_LAB_ROOT for the running platform would otherwise
    redirect every bind mount of a stack started for a different root, so a restore
    would mount and overwrite the source's data.
    """

    def environment(self, ambient):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "clone"
            (root / "config").mkdir(parents=True)
            (root / "config" / "runtime.env").write_text(
                f"COMPOSE_PROJECT_NAME=clone\nDEVOPS_LAB_ROOT={root}\nHTTPS_PORT=9443\n")
            return root, compose.compose_environment(REPOSITORY, root, base=ambient)

    def test_runtime_file_wins_over_a_stale_ambient_root(self) -> None:
        root, environment = self.environment({"DEVOPS_LAB_ROOT": "/home/user/devops-lab", "PATH": "/usr/bin"})
        self.assertEqual(str(root), environment["DEVOPS_LAB_ROOT"])
        self.assertEqual("clone", environment["COMPOSE_PROJECT_NAME"])
        self.assertEqual("/usr/bin", environment["PATH"])
        self.assertIn("GITLAB_IMAGE", environment)

    def test_lab_passes_that_environment_to_every_compose_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "clone"
            (root / "config").mkdir(parents=True)
            (root / "config" / "runtime.env").write_text(
                f"COMPOSE_PROJECT_NAME=clone\nDEVOPS_LAB_ROOT={root}\n")
            (root / "secrets").mkdir()
            (root / "evidence").mkdir()
            lab = compose.Lab(REPOSITORY, root)
            with unittest.mock.patch.object(compose, "run", return_value=b"") as runner:
                lab.dc("ps")
            self.assertEqual(str(root.resolve()), runner.call_args.kwargs["env"]["DEVOPS_LAB_ROOT"])


class VersionsTests(unittest.TestCase):
    def test_repository_versions_pin_five_services_by_digest(self) -> None:
        versions = compose.load_versions(REPOSITORY)
        self.assertEqual(set(versions), set(compose.SERVICES))
        for service, image in versions.items():
            self.assertEqual(64, len(image["digest"]), service)
            self.assertTrue(image["tag"], service)
            self.assertEqual(image["reference"], f"{image['name']}:{image['tag']}@sha256:{image['digest']}")

    def test_unpinned_or_unknown_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = pathlib.Path(temporary)
            good = (REPOSITORY / "versions.env").read_text()
            (repo / "versions.env").write_text(good.replace("@sha256:", "@sha1:", 1))
            with self.assertRaisesRegex(ValueError, "digest"):
                compose.load_versions(repo)
            (repo / "versions.env").write_text(good + "LINUX_AGENT_IMAGE=x:y@sha256:" + "0" * 64 + "\n")
            with self.assertRaisesRegex(ValueError, "unknown"):
                compose.load_versions(repo)
            (repo / "versions.env").write_text("\n".join(good.splitlines()[1:]) + "\n")
            with self.assertRaisesRegex(ValueError, "missing"):
                compose.load_versions(repo)

    def test_read_env_skips_comments_and_rejects_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "runtime.env"
            path.write_text("# comment\nA=1\n\nB=x=y\n")
            self.assertEqual({"A": "1", "B": "x=y"}, compose.read_env(path))
            path.write_text("NOPE\n")
            with self.assertRaises(ValueError):
                compose.read_env(path)


class CommandTests(unittest.TestCase):
    def test_compose_command_reads_versions_then_runtime_and_optional_overlay(self) -> None:
        repo = pathlib.Path("/repo")
        root = pathlib.Path("/runtime")
        self.assertEqual(
            ["docker", "compose", "--env-file", "/repo/versions.env",
             "--env-file", "/runtime/config/runtime.env", "-f", "/repo/compose.yaml"],
            compose.compose_command(repo, root),
        )
        self.assertEqual(
            ["-f", "/runtime/config/overlay.json"],
            compose.compose_command(repo, root, pathlib.Path("/runtime/config/overlay.json"))[-2:],
        )

    def test_compose_command_auto_includes_image_pins_before_an_explicit_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "config").mkdir()
            self.assertNotIn("-f", " ".join(compose.compose_command(REPOSITORY, root)[8:]))
            pins = root / "config" / compose.PINS_FILE
            pins.write_text('{"services": {}}')
            command = compose.compose_command(REPOSITORY, root, pathlib.Path("/x/overlay.json"))
            self.assertEqual(
                ["-f", str(REPOSITORY / "compose.yaml"), "-f", str(pins), "-f", "/x/overlay.json"],
                command[-6:],
            )

    def test_cli_prints_command_one_argument_per_line_and_image_reference(self) -> None:
        printed = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "compose.py"),
             "--repo", str(REPOSITORY), "--root", "/runtime", "command"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(printed, compose.compose_command(REPOSITORY, pathlib.Path("/runtime")))
        image = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "compose.py"),
             "--repo", str(REPOSITORY), "--root", "/runtime", "image", "jenkins"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(image, compose.load_versions(REPOSITORY)["jenkins"]["reference"])

    def test_cli_image_prefers_a_pinned_id_over_the_versions_reference(self) -> None:
        """`scripts/lab` runs the Jenkins image directly to build the Java trust store.

        After an air-gapped restore the versions.env reference is not present locally, so
        that helper has to be told the loaded image id, exactly as Compose is.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "config").mkdir()

            def printed_image() -> str:
                return subprocess.run(
                    [sys.executable, str(REPOSITORY / "scripts" / "compose.py"),
                     "--repo", str(REPOSITORY), "--root", str(root), "image", "jenkins"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            reference = compose.load_versions(REPOSITORY)["jenkins"]["reference"]
            self.assertEqual(reference, printed_image())
            pinned = "sha256:" + "b" * 64
            pins = {"services": {service: {"image": "sha256:" + str(index) * 64, "pull_policy": "never"}
                                 for index, service in enumerate(compose.SERVICES)}}
            pins["services"]["jenkins"]["image"] = pinned
            (root / "config" / compose.PINS_FILE).write_text(json.dumps(pins))
            self.assertEqual(pinned, printed_image())
            (root / "config" / compose.PINS_FILE).unlink()
            self.assertEqual(reference, printed_image())

    def test_run_failure_writes_redacted_private_log_and_hides_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = pathlib.Path(temporary) / "evidence" / "last-failure.log"
            log.parent.mkdir()
            with self.assertRaises(compose.CommandError) as raised:
                compose.run(["sh", "-c", "echo token=hunter2secret >&2; exit 3"],
                            failure_log=log, redactions=[b"hunter2secret"])
            self.assertIn("exit 3", str(raised.exception))
            self.assertNotIn("hunter2secret", str(raised.exception))
            self.assertEqual(b"token=[REDACTED]\n", log.read_bytes())
            self.assertEqual(0o600, log.stat().st_mode & 0o777)

    def test_run_returns_stdout_on_success(self) -> None:
        self.assertEqual(b"ok\n", compose.run(["sh", "-c", "echo ok"]))


@unittest.skipUnless(shutil.which("docker"), "docker not available")
class RenderTests(unittest.TestCase):
    def render_with_ambient(self, root: pathlib.Path, ambient: dict) -> dict:
        rendered = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "compose.py"), "--repo", str(REPOSITORY),
             "--root", str(root), "run", "--", "config", "--format", "json"],
            capture_output=True, text=True, check=True, cwd=REPOSITORY,
            env={**os.environ, **ambient},
        )
        return json.loads(rendered.stdout)

    def test_run_subcommand_ignores_conflicting_ambient_values(self) -> None:
        """`scripts/lab down` under a stale COMPOSE_PROJECT_NAME must not tear down another stack."""
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            subprocess.run(
                [sys.executable, str(REPOSITORY / "scripts" / "bootstrap.py"), "--root", str(root),
                 "--project", "run-test", "--base-domain", "run.test",
                 "--https-port", "8444", "--ssh-port", "2227"],
                check=True, capture_output=True, text=True,
            )
            document = self.render_with_ambient(root, {
                "DEVOPS_LAB_ROOT": "/ambient/root",
                "COMPOSE_PROJECT_NAME": "ambient-project",
                "HTTPS_PORT": "1",
            })
            self.assertEqual("run-test", document["name"])
            sources = [volume["source"] for volume in document["services"]["jenkins"]["volumes"]]
            self.assertTrue(sources)
            for source in sources:
                self.assertTrue(source.startswith(str(root)), source)
                self.assertNotIn("/ambient/root", source)

    def test_compose_renders_exactly_five_services_with_pinned_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            subprocess.run(
                [sys.executable, str(REPOSITORY / "scripts" / "bootstrap.py"), "--root", str(root),
                 "--project", "render-test", "--base-domain", "render.test",
                 "--https-port", "8443", "--ssh-port", "2225"],
                check=True, capture_output=True, text=True,
            )
            # Every operator doc tells them to export DEVOPS_LAB_ROOT, so render under a
            # conflicting one: the env files have to win or this asserts the wrong stack.
            ambient = {**os.environ, "DEVOPS_LAB_ROOT": "/ambient/root"}
            rendered = subprocess.run(
                [*compose.compose_command(REPOSITORY, root), "config", "--format", "json"],
                capture_output=True, text=True, check=True, cwd=REPOSITORY,
                env={**ambient, **compose.compose_environment(REPOSITORY, root)},
            )
            document = json.loads(rendered.stdout)
            services = document["services"]
            self.assertEqual("render-test", document["name"])
            sources = [volume["source"] for volume in services["gitlab"]["volumes"]]
            self.assertTrue(sources)
            for source in sources:
                self.assertTrue(source.startswith(str(root)), source)
            self.assertEqual(set(compose.SERVICES), set(services))
            versions = compose.load_versions(REPOSITORY)
            for service in ("gitlab", "postgres", "sonarqube"):
                self.assertEqual(versions[service]["reference"], services[service]["image"])
            self.assertIn(
                services["gitlab"]["deploy"]["resources"]["limits"]["memory"],
                ("8g", "8589934592"),
            )
            self.assertEqual(f"{os.getuid()}:{os.getgid()}", services["jenkins"]["user"])
            self.assertTrue(services["sonarqube"]["user"].startswith(f"{os.getuid()}:"))
            mounts = {volume["target"]: volume["source"] for volume in services["jenkins"]["volumes"]}
            self.assertEqual(str(root / "secrets" / "jenkins"), mounts["/run/jenkins-secrets"])
            self.assertEqual(str(root / "config" / "jenkins" / "casc.d"), mounts["/run/casc.d"])
            sonar_mounts = {volume["target"] for volume in services["sonarqube"]["volumes"]}
            self.assertIn("/opt/sonarqube/extensions/plugins", sonar_mounts)
            self.assertNotIn("secrets", services["jenkins"])

    def test_image_pins_file_overrides_every_image_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            subprocess.run(
                [sys.executable, str(REPOSITORY / "scripts" / "bootstrap.py"), "--root", str(root),
                 "--project", "pins-test", "--base-domain", "pins.test",
                 "--https-port", "8443", "--ssh-port", "2225"],
                check=True, capture_output=True, text=True,
            )
            pins = {"services": {service: {"image": "sha256:" + str(index) * 64, "pull_policy": "never"}
                                 for index, service in enumerate(compose.SERVICES)}}
            (root / "config" / compose.PINS_FILE).write_text(json.dumps(pins))
            rendered = subprocess.run(
                [*compose.compose_command(REPOSITORY, root), "config", "--format", "json"],
                capture_output=True, text=True, check=True, cwd=REPOSITORY,
                env=compose.compose_environment(REPOSITORY, root),
            )
            services = json.loads(rendered.stdout)["services"]
            for index, service in enumerate(compose.SERVICES):
                self.assertEqual("sha256:" + str(index) * 64, services[service]["image"], service)
                self.assertEqual("never", services[service]["pull_policy"], service)


if __name__ == "__main__":
    unittest.main()
