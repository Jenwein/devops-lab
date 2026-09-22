import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP = REPOSITORY / "scripts" / "bootstrap.py"


def run_bootstrap(root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--root",
            str(root),
            "--project",
            "test-lab",
            "--base-domain",
            "example.test",
            "--https-port",
            "8443",
            "--ssh-port",
            "2225",
            *extra,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
    )


class BootstrapTests(unittest.TestCase):
    def test_setup_refuses_nonempty_unmarked_root_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "existing"
            root.mkdir(mode=0o755)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("leave me alone\n")
            original_mode = root.stat().st_mode & 0o777

            result = run_bootstrap(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-empty", result.stderr.lower())
            self.assertEqual(sentinel.read_text(), "leave me alone\n")
            self.assertEqual(root.stat().st_mode & 0o777, original_mode)
            self.assertEqual([sentinel], list(root.iterdir()))

    def test_new_passwords_meet_character_classes_without_rotating_existing(self):
        from unittest.mock import patch
        sys.path.insert(0, str(REPOSITORY / 'scripts'))
        import bootstrap
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / 'sonar_admin_password'
            with patch('bootstrap.secrets.token_urlsafe', return_value='a'*43):
                bootstrap.ensure_secret(path)
            value = path.read_text().strip()
            self.assertTrue(any(c.isupper() for c in value))
            self.assertTrue(any(c.islower() for c in value))
            self.assertTrue(any(c.isdigit() for c in value))
            self.assertTrue(any(not c.isalnum() for c in value))
            path.write_text('existing-active-value\n')
            bootstrap.ensure_secret(path)
            self.assertEqual(path.read_text(), 'existing-active-value\n')

    def test_empty_setup_accepts_gitlab_trusted_certificate_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            result = run_bootstrap(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = subprocess.run(["install", "-m", "0644", str(root / "tls/ca.crt"),
                str(root / "data/gitlab/config/trusted-certs/devops-lab.crt")], capture_output=True)
            self.assertEqual(installed.returncode, 0, installed.stderr)

    def test_setup_creates_private_runtime_and_rendered_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"

            result = run_bootstrap(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            for secret_file in (root / "secrets").rglob("*"):
                if secret_file.is_file():
                    secret = secret_file.read_text()
                    self.assertNotIn(secret, result.stdout, secret_file.name)
                    self.assertNotIn(secret, result.stderr, secret_file.name)
                    self.assertFalse(secret.endswith("\n"), secret_file.name)
                    self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600, secret_file.name)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for directory in (
                "secrets", "secrets/jenkins", "tls", "tls/trust", "config",
                "config/jenkins/casc.d", "config/sonarqube/plugins", "data", "evidence", "backups",
            ):
                self.assertTrue((root / directory).is_dir(), directory)
                self.assertEqual((root / directory).stat().st_mode & 0o777, 0o700, directory)
            self.assertEqual(
                {"gitlab_root_password", "sonar_db_password", "sonar_admin_password", "jenkins"},
                {path.name for path in (root / "secrets").iterdir()},
            )
            self.assertEqual(
                {"jenkins_admin_password", "gitlab_webhook_secret"},
                {path.name for path in (root / "secrets" / "jenkins").iterdir()},
            )
            runtime_env = root / "config" / "runtime.env"
            self.assertEqual(runtime_env.stat().st_mode & 0o777, 0o600)
            settings = runtime_env.read_text()
            for line in (
                "COMPOSE_PROJECT_NAME=test-lab",
                "GITLAB_HOST=gitlab.example.test",
                "HTTPS_PORT=8443",
                "GITLAB_URL=https://gitlab.example.test:8443",
                "JENKINS_URL=https://jenkins.example.test:8443",
                "BIND_ADDRESS=0.0.0.0",
                f"DEVOPS_LAB_UID={os.getuid()}",
                f"DEVOPS_LAB_GID={os.getgid()}",
                "EDGE_MEMORY_LIMIT=256m",
                "GITLAB_MEMORY_LIMIT=8g",
                "JENKINS_MEMORY_LIMIT=3g",
                "POSTGRES_MEMORY_LIMIT=2g",
                "SONARQUBE_MEMORY_LIMIT=5g",
            ):
                self.assertIn(line + "\n", settings)
            self.assertNotIn("PASSWORD", settings.upper())

    def test_setup_preserves_credentials_and_ca_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            first = run_bootstrap(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            protected = [
                root / "secrets" / "gitlab_root_password",
                root / "secrets" / "sonar_db_password",
                root / "secrets" / "sonar_admin_password",
                root / "secrets" / "jenkins" / "jenkins_admin_password",
                root / "secrets" / "jenkins" / "gitlab_webhook_secret",
                root / "tls" / "ca.key",
                root / "tls" / "ca.crt",
            ]
            before = {path: path.read_bytes() for path in protected}

            second = run_bootstrap(root)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(before, {path: path.read_bytes() for path in protected})
            for path in protected:
                expected = 0o644 if path.name == "ca.crt" else 0o600
                self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))

    def test_rerun_keeps_edited_values_and_applies_only_explicit_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            self.assertEqual(run_bootstrap(root).returncode, 0)
            runtime_env = root / "config" / "runtime.env"
            edited = runtime_env.read_text().replace("GITLAB_MEMORY_LIMIT=8g", "GITLAB_MEMORY_LIMIT=4g")
            runtime_env.write_text(edited)

            result = subprocess.run(
                [sys.executable, str(BOOTSTRAP), "--root", str(root), "--https-port", "9443"],
                cwd=REPOSITORY, capture_output=True, text=True,
                env={key: value for key, value in os.environ.items() if not key.startswith("DEVOPS_LAB_")},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            settings = runtime_env.read_text()
            self.assertIn("GITLAB_MEMORY_LIMIT=4g\n", settings)
            self.assertIn("HTTPS_PORT=9443\n", settings)
            self.assertIn("GITLAB_URL=https://gitlab.example.test:9443\n", settings)
            self.assertIn("COMPOSE_PROJECT_NAME=test-lab\n", settings)
            self.assertIn("GITLAB_SSH_PORT=2225\n", settings)

    def test_rerun_preserves_unknown_runtime_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            self.assertEqual(run_bootstrap(root).returncode, 0)
            runtime_env = root / "config" / "runtime.env"
            with runtime_env.open("a") as handle:
                handle.write("CUSTOM_SETTING=keep-me\n")

            result = run_bootstrap(root, "--https-port", "9443")

            self.assertEqual(result.returncode, 0, result.stderr)
            settings = runtime_env.read_text()
            self.assertIn("CUSTOM_SETTING=keep-me\n", settings)
            self.assertIn("HTTPS_PORT=9443\n", settings)

    def test_first_run_takes_site_values_from_environment_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            result = subprocess.run(
                [sys.executable, str(BOOTSTRAP), "--root", str(root)],
                cwd=REPOSITORY, capture_output=True, text=True,
                env=os.environ | {"DEVOPS_LAB_PROJECT": "env-lab", "DEVOPS_LAB_DOMAIN": "env.test",
                                  "DEVOPS_LAB_BIND": "127.0.0.1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = (root / "config" / "runtime.env").read_text()
            self.assertIn("COMPOSE_PROJECT_NAME=env-lab\n", settings)
            self.assertIn("SONAR_HOST=sonar.env.test\n", settings)
            self.assertIn("BIND_ADDRESS=127.0.0.1\n", settings)
            self.assertIn("HTTPS_PORT=443\n", settings)
            self.assertIn("GITLAB_URL=https://gitlab.env.test\n", settings)

    def test_setup_rejects_invalid_bind_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            result = run_bootstrap(root, "--bind-address", "not-an-address")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bind", result.stderr.lower())
            self.assertFalse(root.exists())

    def test_setup_regenerates_leaf_certificate_for_changed_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            first = run_bootstrap(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            ca_before = (root / "tls" / "ca.crt").read_bytes()
            leaf_before = (root / "tls" / "server.crt").read_bytes()

            second = run_bootstrap(root, "--base-domain", "changed.test")

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(ca_before, (root / "tls" / "ca.crt").read_bytes())
            self.assertNotEqual(leaf_before, (root / "tls" / "server.crt").read_bytes())
            for service in ("gitlab", "jenkins", "sonar"):
                checked = subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-CAfile",
                        str(root / "tls" / "ca.crt"),
                        "-verify_hostname",
                        f"{service}.changed.test",
                        str(root / "tls" / "server.crt"),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            old_host = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    str(root / "tls" / "ca.crt"),
                    "-verify_hostname",
                    "gitlab.example.test",
                    str(root / "tls" / "server.crt"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(old_host.returncode, 0)
            settings = (root / "config" / "runtime.env").read_text()
            self.assertIn("GITLAB_HOST=gitlab.changed.test", settings)

    def test_setup_rejects_invalid_ports_and_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            invalid_port = run_bootstrap(root, "--https-port", "70000")
            invalid_domain = run_bootstrap(root, "--base-domain", "not a domain")

            self.assertNotEqual(invalid_port.returncode, 0)
            self.assertIn("port", invalid_port.stderr.lower())
            self.assertNotEqual(invalid_domain.returncode, 0)
            self.assertIn("domain", invalid_domain.stderr.lower())
            self.assertFalse(root.exists())

    def test_setup_refuses_existing_runtime_owned_by_another_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"
            first = run_bootstrap(root)
            self.assertEqual(first.returncode, 0, first.stderr)

            conflict = run_bootstrap(root, "--project", "another-lab")

            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("project", conflict.stderr.lower())


if __name__ == "__main__":
    unittest.main()
