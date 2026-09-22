import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests"))
import auth  # noqa: E402
from fakes import FakeTransport  # noqa: E402


class ApplicationTests(unittest.TestCase):
    def test_existing_application_with_stored_secret_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret = pathlib.Path(temporary) / "secret"
            secret.write_text("stored")
            gitlab = FakeTransport({("GET", "/applications"): (200, [
                {"id": 3, "application_name": "devops-lab-jenkins", "application_id": "abc",
                 "callback_url": "https://j/securityRealm/finishLogin"}])})
            self.assertEqual(("abc", "kept"), auth.ensure_application(
                gitlab, "devops-lab-jenkins", "https://j/securityRealm/finishLogin", "openid profile email", secret))
            self.assertEqual([], gitlab.posts())

    def test_missing_secret_or_changed_callback_recreates_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret = pathlib.Path(temporary) / "secret"
            gitlab = FakeTransport({
                ("GET", "/applications"): (200, [{"id": 3, "application_name": "devops-lab-jenkins",
                                                   "application_id": "old", "callback_url": "https://j/securityRealm/finishLogin"}]),
                ("DELETE", "/applications/3"): (204, {}),
                ("POST", "/applications"): (201, {"application_id": "new", "secret": "s3cret-value"}),
            })
            self.assertEqual(("new", "created"), auth.ensure_application(
                gitlab, "devops-lab-jenkins", "https://j/securityRealm/finishLogin", "openid profile email", secret))
            self.assertEqual("s3cret-value", secret.read_text())
            self.assertEqual(0o600, secret.stat().st_mode & 0o777)
            self.assertEqual([("DELETE", "/applications/3", None),
                              ("POST", "/applications", {"name": "devops-lab-jenkins", "redirect_uri": "https://j/securityRealm/finishLogin",
                                                         "scopes": "openid profile email", "confidential": "true"})], gitlab.posts())


class OverlayTests(unittest.TestCase):
    def test_overlay_references_secret_variables_not_values(self) -> None:
        text = auth.render_jenkins_overlay("https://gitlab.example.test", "client-id-1")
        self.assertIn("client-id-1", text)
        self.assertIn("${GITLAB_OIDC_CLIENT_SECRET}", text)
        self.assertIn("${JENKINS_ADMIN_PASSWORD}", text)
        self.assertIn("https://gitlab.example.test/.well-known/openid-configuration", text)
        self.assertIn("securityRealm:", text)
        self.assertIn("groups", text)


class SonarConfigurationTests(unittest.TestCase):
    """SonarQube 26 rejects these keys through api/settings/set and requires the v2 endpoint."""

    PATH = "/api/v2/dop-translation/gitlab-configurations"

    def configured(self, **overrides):
        current = {"id": "gitlab-configuration", "enabled": True, "applicationId": "app-id",
                   "url": "https://gitlab.example.test", "synchronizeGroups": True,
                   "allowUsersToSignUp": True, "provisioningType": "JIT"}
        current.update(overrides)
        return FakeTransport({("GET", self.PATH): (200, {"gitlabConfigurations": [current]}),
                              ("PATCH", f"{self.PATH}/gitlab-configuration"): (204, {})})

    def test_first_run_creates_the_configuration(self) -> None:
        sonar = FakeTransport({("GET", self.PATH): (200, {"gitlabConfigurations": []}),
                               ("POST", self.PATH): (201, {})})
        self.assertEqual("created", auth.configure_sonar_gitlab_auth(
            sonar, "https://gitlab.example.test", "app-id", "app-secret"))
        method, path, body = sonar.posts()[0]
        self.assertEqual(("POST", self.PATH), (method, path))
        sent = json.loads(body)
        self.assertEqual({"enabled": True, "applicationId": "app-id", "url": "https://gitlab.example.test",
                          "secret": "app-secret", "synchronizeGroups": True, "allowUsersToSignUp": True,
                          "provisioningType": "JIT", "allowedGroups": []}, sent)

    def test_existing_configuration_is_patched_and_never_sent_to_settings_set(self) -> None:
        sonar = self.configured(url="https://old.example.test")
        self.assertEqual("updated", auth.configure_sonar_gitlab_auth(
            sonar, "https://gitlab.example.test", "app-id", "app-secret", application_changed=False))
        method, path, body = sonar.posts()[0]
        self.assertEqual(("PATCH", f"{self.PATH}/gitlab-configuration"), (method, path))
        self.assertEqual("https://gitlab.example.test", json.loads(body)["url"])
        self.assertNotIn("/api/settings/set", [call[1] for call in sonar.calls])

    def test_matching_configuration_with_an_unchanged_application_is_kept(self) -> None:
        sonar = self.configured()
        self.assertEqual("kept", auth.configure_sonar_gitlab_auth(
            sonar, "https://gitlab.example.test", "app-id", "app-secret", application_changed=False))
        self.assertEqual([], sonar.posts())

    def test_recreated_application_rewrites_the_unreadable_secret(self) -> None:
        sonar = self.configured()
        self.assertEqual("updated", auth.configure_sonar_gitlab_auth(
            sonar, "https://gitlab.example.test", "app-id", "app-secret", application_changed=True))
        self.assertEqual("app-secret", json.loads(sonar.posts()[0][2])["secret"])


class EnableTests(unittest.TestCase):
    def make_lab(self, root: pathlib.Path) -> mock.Mock:
        for name in ("secrets/jenkins", "config/jenkins/casc.d", "evidence"):
            (root / name).mkdir(parents=True)
        lab = mock.Mock()
        lab.root = root
        lab.env = {"GITLAB_URL": "https://gitlab.example.test", "JENKINS_URL": "https://jenkins.example.test",
                   "SONAR_URL": "https://sonar.example.test"}
        lab.gitlab.return_value = FakeTransport({
            ("GET", "/applications"): (200, []),
            ("POST", "/applications"): [(201, {"application_id": "sonar-id", "secret": "sonar-secret"}),
                                        (201, {"application_id": "jenkins-id", "secret": "jenkins-secret"})],
        })
        lab.sonar.return_value = FakeTransport({
            ("GET", "/api/v2/dop-translation/gitlab-configurations"): (200, {"gitlabConfigurations": []}),
            ("POST", "/api/v2/dop-translation/gitlab-configurations"): (201, {})})
        return lab

    def test_enable_writes_secrets_overlay_and_restarts_jenkins_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            lab = self.make_lab(root)
            with mock.patch.object(auth, "wait_for_jenkins") as wait:
                report = auth.enable(lab)
            self.assertEqual("created", report["sonar_application"])
            self.assertEqual("created", report["jenkins_application"])
            self.assertEqual("restarted", report["jenkins"])
            self.assertEqual("sonar-secret", (root / "secrets" / "sonar_gitlab_oauth_secret").read_text())
            self.assertEqual("jenkins-secret", (root / "secrets" / "jenkins" / "gitlab_oidc_client_secret").read_text())
            overlay = root / "config" / "jenkins" / "casc.d" / "50-gitlab-auth.yaml"
            self.assertIn("jenkins-id", overlay.read_text())
            self.assertNotIn("jenkins-secret", overlay.read_text())
            lab.dc.assert_called_once_with("restart", "jenkins", timeout=600)
            wait.assert_called_once()

    def test_rerun_without_changes_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            lab = self.make_lab(root)
            with mock.patch.object(auth, "wait_for_jenkins"):
                auth.enable(lab)
            lab.dc.reset_mock()
            lab.gitlab.return_value = FakeTransport({("GET", "/applications"): (200, [
                {"id": 1, "application_name": "devops-lab-sonarqube", "application_id": "sonar-id",
                 "callback_url": "https://sonar.example.test/oauth2/callback/gitlab"},
                {"id": 2, "application_name": "devops-lab-jenkins", "application_id": "jenkins-id",
                 "callback_url": "https://jenkins.example.test/securityRealm/finishLogin"}])})
            with mock.patch.object(auth, "wait_for_jenkins"):
                report = auth.enable(lab)
            self.assertEqual("kept", report["jenkins"])
            lab.dc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
