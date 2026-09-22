import datetime
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests"))
import platform_init as platform  # noqa: E402
from fakes import FakeTransport  # noqa: E402


def fake_lab(root: pathlib.Path) -> mock.Mock:
    lab = mock.Mock()
    lab.root = root
    lab.env = {
        "GITLAB_URL": "https://gitlab.example.test", "GITLAB_HOST": "gitlab.example.test",
        "JENKINS_URL": "https://jenkins.example.test:8443", "JENKINS_HOST": "jenkins.example.test",
        "SONAR_URL": "https://sonar.example.test", "SONAR_HOST": "sonar.example.test",
    }
    lab.secret.side_effect = lambda name: {"sonar_admin_password": "Aa1!stored", "gitlab_root_token": "glpat-stored"}[name]
    return lab


class SonarPasswordTests(unittest.TestCase):
    def test_stored_password_valid_means_kept(self) -> None:
        lab = fake_lab(pathlib.Path("/r"))
        lab.sonar.return_value = FakeTransport({("GET", "/api/authentication/validate"): (200, {"valid": True})})
        self.assertEqual("kept", platform.ensure_sonar_admin_password(lab))
        lab.transport.assert_not_called()

    def test_default_password_is_rotated_to_stored_value(self) -> None:
        lab = fake_lab(pathlib.Path("/r"))
        stored = FakeTransport({("GET", "/api/authentication/validate"): [(200, {"valid": False}), (200, {"valid": True})]})
        initial = FakeTransport({("GET", "/api/authentication/validate"): (200, {"valid": True}),
                                 ("POST", "/api/users/change_password"): (204, {})})
        lab.sonar.return_value = stored
        lab.transport.return_value = initial
        self.assertEqual("rotated", platform.ensure_sonar_admin_password(lab))
        method, path, fields = initial.posts()[0]
        self.assertEqual(("POST", "/api/users/change_password"), (method, path))
        self.assertEqual({"login": "admin", "previousPassword": "admin", "password": "Aa1!stored"}, fields)

    def test_unknown_state_fails_clearly(self) -> None:
        lab = fake_lab(pathlib.Path("/r"))
        lab.sonar.return_value = FakeTransport({("GET", "/api/authentication/validate"): (200, {"valid": False})})
        lab.transport.return_value = FakeTransport({("GET", "/api/authentication/validate"): (401, {})})
        with self.assertRaisesRegex(RuntimeError, "neither"):
            platform.ensure_sonar_admin_password(lab)


class SonarSettingsTests(unittest.TestCase):
    def test_setting_and_webhook_are_written_only_when_different(self) -> None:
        client = FakeTransport({
            ("GET", "/api/settings/values"): (200, {"settings": [{"key": "sonar.core.serverBaseURL", "value": "https://old"}]}),
            ("POST", "/api/settings/set"): (204, {}),
            ("GET", "/api/webhooks/list"): (200, {"webhooks": [{"key": "k1", "name": "jenkins", "url": "https://old/sonarqube-webhook/"}]}),
            ("POST", "/api/webhooks/update"): (204, {}),
        })
        self.assertEqual("updated", platform.ensure_sonar_setting(client, "sonar.core.serverBaseURL", "https://sonar.example.test"))
        self.assertEqual("updated", platform.ensure_sonar_webhook(client, "jenkins", "https://jenkins.example.test:8443/sonarqube-webhook/"))
        self.assertEqual([("POST", "/api/settings/set", {"key": "sonar.core.serverBaseURL", "value": "https://sonar.example.test"}),
                          ("POST", "/api/webhooks/update", {"webhook": "k1", "name": "jenkins", "url": "https://jenkins.example.test:8443/sonarqube-webhook/"})],
                         client.posts())
        same = FakeTransport({
            ("GET", "/api/settings/values"): (200, {"settings": [{"key": "sonar.core.serverBaseURL", "value": "https://sonar.example.test"}]}),
            ("GET", "/api/webhooks/list"): (200, {"webhooks": [{"key": "k1", "name": "jenkins", "url": "https://j/sonarqube-webhook/"}]}),
        })
        self.assertEqual("kept", platform.ensure_sonar_setting(same, "sonar.core.serverBaseURL", "https://sonar.example.test"))
        self.assertEqual("kept", platform.ensure_sonar_webhook(same, "jenkins", "https://j/sonarqube-webhook/"))
        self.assertEqual([], same.posts())
        missing = FakeTransport({("GET", "/api/webhooks/list"): (200, {"webhooks": []}), ("POST", "/api/webhooks/create"): (200, {})})
        self.assertEqual("created", platform.ensure_sonar_webhook(missing, "jenkins", "https://j/sonarqube-webhook/"))


class GitLabTokenTests(unittest.TestCase):
    def test_renewal_decision(self) -> None:
        today = datetime.date(2026, 9, 21)
        self.assertTrue(platform.token_needs_renewal(None, today))
        self.assertTrue(platform.token_needs_renewal("2026-10-01", today))
        self.assertFalse(platform.token_needs_renewal("2027-09-01", today))

    def test_valid_stored_token_is_kept_and_expiring_token_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "secrets").mkdir()
            (root / "secrets" / "gitlab_root_token").write_text("glpat-stored")
            lab = fake_lab(root)
            lab.gitlab.return_value = FakeTransport({("GET", "/personal_access_tokens/self"): (200, {"expires_at": "2027-09-01"})})
            self.assertEqual("kept", platform.ensure_gitlab_root_token(lab, today=datetime.date(2026, 9, 21)))
            lab.exec.assert_not_called()
            lab.gitlab.return_value = FakeTransport({("GET", "/personal_access_tokens/self"): (200, {"expires_at": "2026-09-30"})})
            lab.exec.return_value = b"warning: something\nglpat-newvalue1234567890\n"
            self.assertEqual("created", platform.ensure_gitlab_root_token(lab, today=datetime.date(2026, 9, 21)))
            self.assertEqual("glpat-newvalue1234567890", (root / "secrets" / "gitlab_root_token").read_text())
            self.assertEqual(0o600, (root / "secrets" / "gitlab_root_token").stat().st_mode & 0o777)
            runner = lab.exec.call_args.args
            self.assertEqual(("gitlab", "gitlab-rails", "runner"), runner[:3])
            self.assertIn("devops-lab-platform", runner[3])
            self.assertNotIn("glpat", runner[3])

    def test_missing_token_file_creates_without_probing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "secrets").mkdir()
            lab = fake_lab(root)
            lab.exec.return_value = b"glpat-created12345678901234\n"
            self.assertEqual("created", platform.ensure_gitlab_root_token(lab, today=datetime.date(2026, 9, 21)))
            lab.gitlab.assert_not_called()

    def test_routable_token_with_dotted_suffix_is_accepted(self) -> None:
        """GitLab 19 returns `glpat-<secret>.<version>.<routing>`; the dots are part of the token."""
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "secrets").mkdir()
            lab = fake_lab(root)
            lab.exec.return_value = b"glpat-abcdefghijklmnopqrst.01.0w00mnl68\n"
            self.assertEqual("created", platform.ensure_gitlab_root_token(lab, today=datetime.date(2026, 9, 21)))
            self.assertEqual("glpat-abcdefghijklmnopqrst.01.0w00mnl68",
                             (root / "secrets" / "gitlab_root_token").read_text())

    def test_non_token_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "secrets").mkdir()
            lab = fake_lab(root)
            lab.exec.return_value = b"could not find user root\n"
            with self.assertRaises(RuntimeError):
                platform.ensure_gitlab_root_token(lab, today=datetime.date(2026, 9, 21))


class AllowlistTests(unittest.TestCase):
    def test_jenkins_host_and_port_are_added_once(self) -> None:
        lab = fake_lab(pathlib.Path("/r"))
        client = FakeTransport({("GET", "/application/settings"): [
            (200, {"outbound_local_requests_whitelist": ["other.test"]}),
            (200, {"outbound_local_requests_whitelist": ["other.test", "jenkins.example.test:8443"]}),
        ], ("PUT", "/application/settings"): (200, {})})
        self.assertEqual("updated", platform.ensure_gitlab_outbound_allowlist(lab, client))
        self.assertEqual([("PUT", "/application/settings", {"outbound_local_requests_whitelist[]": ["other.test", "jenkins.example.test:8443"]})], client.posts())
        present = FakeTransport({("GET", "/application/settings"): (200, {"outbound_local_requests_whitelist": ["jenkins.example.test:8443"]})})
        self.assertEqual("kept", platform.ensure_gitlab_outbound_allowlist(lab, present))


if __name__ == "__main__":
    unittest.main()
