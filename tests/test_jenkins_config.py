import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
ENTRYPOINT = REPOSITORY / "config" / "jenkins" / "jenkins-entrypoint.sh"
CASC = REPOSITORY / "config" / "jenkins" / "casc.yaml"
LOCAL_REALM = REPOSITORY / "config" / "jenkins" / "casc-local-realm.yaml"
ALLOWED_VARIABLES = {"JENKINS_URL", "GITLAB_URL", "SONAR_URL", "JENKINS_ADMIN_PASSWORD", "GITLAB_WEBHOOK_SECRET"}


class EntrypointTests(unittest.TestCase):
    def run_entrypoint(self, base: pathlib.Path, overlay_files: dict[str, str]) -> dict[str, str]:
        secrets = base / "secrets"
        overlay = base / "casc.d"
        secrets.mkdir()
        overlay.mkdir()
        (secrets / "jenkins_admin_password").write_text("Aa1!adminvalue")
        (secrets / "gitlab_webhook_secret").write_text("hooksecret")
        (secrets / "gitlab-oidc-client-secret").write_text("oidc")
        for name, content in overlay_files.items():
            (overlay / name).write_text(content)
        launcher = base / "launcher.sh"
        launcher.write_text("#!/bin/sh\nenv\n")
        launcher.chmod(0o755)
        result = subprocess.run(
            ["sh", str(ENTRYPOINT)],
            env={
                "PATH": os.environ["PATH"],
                "JENKINS_URL": "https://jenkins.example.test",
                "JENKINS_HOST": "jenkins.example.test",
                "JENKINS_SECRETS_DIR": str(secrets),
                "CASC_OVERLAY_DIR": str(overlay),
                "CASC_CORE_FILE": "/core/casc.yaml",
                "CASC_LOCAL_REALM_FILE": "/core/casc-local-realm.yaml",
                "JENKINS_LAUNCHER": str(launcher),
            },
            capture_output=True, text=True, check=True,
        )
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_secrets_become_environment_and_overlay_directory_is_added_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self.run_entrypoint(pathlib.Path(temporary), {"10-auth.yaml": "jenkins: {}\n"})
            self.assertEqual("Aa1!adminvalue", env["JENKINS_ADMIN_PASSWORD"])
            self.assertEqual("hooksecret", env["GITLAB_WEBHOOK_SECRET"])
            self.assertEqual("oidc", env["GITLAB_OIDC_CLIENT_SECRET"])
            self.assertEqual("/core/casc.yaml,/core/casc-local-realm.yaml," + temporary + "/casc.d",
                             env["CASC_JENKINS_CONFIG"])
            self.assertEqual("override", env["CASC_MERGE_STRATEGY"])

    def test_empty_overlay_directory_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self.run_entrypoint(pathlib.Path(temporary), {"README": "not yaml"})
            self.assertEqual("/core/casc.yaml,/core/casc-local-realm.yaml", env["CASC_JENKINS_CONFIG"])

    def test_an_overlay_realm_replaces_the_local_one(self) -> None:
        """Two sources naming a realm merge into one with two entries, which Jenkins rejects."""
        with tempfile.TemporaryDirectory() as temporary:
            env = self.run_entrypoint(pathlib.Path(temporary), {
                "50-gitlab-auth.yaml": "jenkins:\n  securityRealm:\n    oic:\n      clientId: \"x\"\n"})
            self.assertEqual("/core/casc.yaml," + temporary + "/casc.d", env["CASC_JENKINS_CONFIG"])

    def test_a_non_yaml_file_naming_a_realm_does_not_drop_the_local_one(self) -> None:
        """Only the files the include loop picks up can replace the realm."""
        with tempfile.TemporaryDirectory() as temporary:
            env = self.run_entrypoint(pathlib.Path(temporary), {
                "notes.txt": "securityRealm: see the runbook\n",
                "10-auth.yaml": "jenkins: {}\n"})
            self.assertEqual("/core/casc.yaml,/core/casc-local-realm.yaml," + temporary + "/casc.d",
                             env["CASC_JENKINS_CONFIG"])

    def test_invalid_origin_is_refused_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            (base / "secrets").mkdir()
            (base / "casc.d").mkdir()
            result = subprocess.run(
                ["sh", str(ENTRYPOINT)],
                env={"PATH": os.environ["PATH"], "JENKINS_URL": "http://jenkins.example.test",
                     "JENKINS_HOST": "jenkins.example.test", "JENKINS_SECRETS_DIR": str(base / "secrets"),
                     "CASC_OVERLAY_DIR": str(base / "casc.d"), "JENKINS_LAUNCHER": "/bin/false"},
                capture_output=True, text=True,
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("refusing", result.stderr)


class CascTests(unittest.TestCase):
    def test_casc_uses_only_variables_the_entrypoint_provides(self) -> None:
        used = set(re.findall(r"\$\{([A-Z_]+)\}", CASC.read_text()))
        self.assertTrue(used, "casc.yaml should reference runtime variables")
        self.assertEqual(set(), used - ALLOWED_VARIABLES)

    def test_only_the_realm_file_declares_a_security_realm(self) -> None:
        self.assertNotIn("securityRealm", CASC.read_text())
        realm = LOCAL_REALM.read_text()
        self.assertIn("securityRealm:", realm)
        self.assertIn("${JENKINS_ADMIN_PASSWORD}", realm)
        self.assertEqual(set(), set(re.findall(r"\$\{([A-Z_]+)\}", realm)) - ALLOWED_VARIABLES)

    def test_casc_declares_role_based_authorization_and_platform_servers(self) -> None:
        text = CASC.read_text()
        self.assertIn("roleBased:", text)
        self.assertNotIn("loggedInUsersCanDoAnything", text)
        self.assertIn("numExecutors: 0", text)
        self.assertIn("slaveAgentPort: -1", text)
        self.assertIn("name: platform", text)
        self.assertIn("gitlab-webhook-secret", text)

    def test_plugins_are_pinned_and_include_required_set(self) -> None:
        """The file pins the whole resolved set, so a rebuild cannot drift; comments split the blocks."""
        raw = [line.strip() for line in (REPOSITORY / "config/jenkins/plugins.txt").read_text().splitlines()]
        lines = [line for line in raw if line and not line.startswith("#")]
        listed = [line.split(":", 1)[0] for line in lines]
        names = set(listed)
        for line in lines:
            self.assertRegex(line, r"^[a-z0-9-]+:[A-Za-z0-9._-]+$", line)
        duplicates = sorted({name for name in listed if listed.count(name) > 1})
        self.assertEqual([], duplicates, f"a plugin may be pinned once: {duplicates}")
        for required in ("configuration-as-code", "role-strategy", "cloudbees-folder", "gitlab-branch-source",
                         "sonar", "workflow-aggregator", "credentials-binding", "git", "oic-auth"):
            self.assertIn(required, names)
        for retired in ("ssh-slaves", "nodejs", "job-dsl", "gitlab-plugin"):
            self.assertNotIn(retired, names)


if __name__ == "__main__":
    unittest.main()
