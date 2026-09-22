import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
LAB = REPOSITORY / "scripts" / "lab"


def run_lab(*arguments: str, root: pathlib.Path, extra_env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", str(LAB), *arguments],
        cwd=REPOSITORY,
        env=os.environ | {"DEVOPS_LAB_ROOT": str(root)} | (extra_env or {}),
        capture_output=True,
        text=True,
    )


class LabCliTests(unittest.TestCase):
    def test_retired_commands_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            for command in ("init-example", "smoke", "prepare-assets", "init"):
                with self.subTest(command=command):
                    result = run_lab(command, root=root)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage", result.stderr.lower())
                    self.assertFalse(root.exists())

    def test_usage_lists_only_core_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_lab(root=pathlib.Path(temporary) / "runtime")
            self.assertEqual(result.returncode, 2)
            for command in ("setup", "up", "down", "status", "backup", "restore", "add-team", "enable-gitlab-auth"):
                self.assertIn(command, result.stderr)
            for retired in ("init-example", "smoke", "prepare-assets"):
                self.assertNotIn(retired, result.stderr)

    def test_setup_uses_environment_defaults_and_forwards_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            result = run_lab(
                "setup",
                root=root,
                extra_env={
                    "DEVOPS_LAB_PROJECT": "shell-test",
                    "DEVOPS_LAB_DOMAIN": "shell.test",
                    "DEVOPS_LAB_HTTPS_PORT": "9443",
                    "DEVOPS_LAB_SSH_PORT": "2226",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (root / "config" / "runtime.env").read_text()
            self.assertIn("COMPOSE_PROJECT_NAME=shell-test", rendered)
            self.assertIn("SONAR_HOST=sonar.shell.test", rendered)

    def test_commands_refuse_an_uninitialised_root(self) -> None:
        """`up` against a fresh root used to bootstrap a new CA and new secrets silently."""
        for command in ("status", "down"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary) / "runtime"
                result = run_lab(command, root=root)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn(f"no runtime configuration at {root / 'config' / 'runtime.env'}", result.stderr)
                self.assertIn(f"run 'scripts/lab setup --root {root}' or export DEVOPS_LAB_ROOT", result.stderr)
                self.assertFalse(root.exists())

    def test_unknown_command_exits_with_usage_without_creating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            result = run_lab("destroy-everything", root=root)
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage", result.stderr.lower())
            self.assertFalse(root.exists())

    def test_up_builds_only_on_request(self) -> None:
        """`up` used to force `--build` on every run; images pinned by a restore must survive it."""
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = pathlib.Path(temporary)
            root = temporary_path / "runtime"
            (root / "config").mkdir(parents=True)
            (root / "config" / "runtime.env").write_text(f"DEVOPS_LAB_ROOT={root}\nCOMPOSE_PROJECT_NAME=x\n")
            (root / "tls" / "trust").mkdir(parents=True)
            (root / "tls" / "trust" / "java-cacerts.p12").write_bytes(b"")
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            recorded = temporary_path / "docker-args"
            (fake_bin / "docker").write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$DOCKER_ARGS\"\nexit 0\n")
            (fake_bin / "docker").chmod(0o755)
            (fake_bin / "python3").write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                f"  *compose.py*) exec {sys.executable} \"$@\" ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            (fake_bin / "python3").chmod(0o755)
            env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "DOCKER_ARGS": str(recorded)}
            for extra, expected in (((), False), (("--build",), True)):
                with self.subTest(extra=extra):
                    recorded.write_text("")
                    result = run_lab("up", *extra, root=root, extra_env=env)
                    self.assertEqual(0, result.returncode, result.stderr)
                    arguments = recorded.read_text().split("\n")
                    self.assertIn("up", arguments)
                    self.assertEqual(expected, "--build" in arguments)
                    self.assertIn("--wait", arguments)


if __name__ == "__main__":
    unittest.main()
