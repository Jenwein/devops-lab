import pathlib
import sys
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests"))
import teams  # noqa: E402
from fakes import FakeTransport  # noqa: E402

TODAY = __import__("datetime").date(2026, 9, 21)


def fresh_gitlab() -> FakeTransport:
    return FakeTransport({
        ("GET", "/groups"): (200, []),
        ("POST", "/groups"): (201, {"id": 7, "full_path": "demo"}),
        ("GET", "/groups/7/access_tokens"): (200, []),
        ("POST", "/groups/7/access_tokens"): (201, {"id": 1, "token": "glgat-team-token-value"}),
        ("GET", "/users"): (200, [{"id": 42, "username": "alice"}]),
        ("GET", "/groups/7/members/all/42"): (404, {}),
        ("POST", "/groups/7/members"): (201, {}),
    })


def fresh_jenkins() -> FakeTransport:
    return FakeTransport({
        ("GET", "/job/demo/api/json"): [(404, {}), (200, {})],
        ("POST", "/createItem"): (200, {}),
        ("GET", "/role-strategy/strategy/getRole"): (200, {}),
        ("POST", "/role-strategy/strategy/addRole"): (200, {}),
        ("POST", "/role-strategy/strategy/assignGroupRole"): (200, {}),
        ("POST", "/role-strategy/strategy/assignUserRole"): (200, {}),
        ("GET", "/job/demo/credentials/store/folder/domain/_/credential/gitlab-demo/api/json"): (404, {}),
        ("GET", "/job/demo/credentials/store/folder/domain/_/credential/sonar-demo/api/json"): (404, {}),
        ("POST", "/job/demo/credentials/store/folder/domain/_/createCredentials"): (200, {}),
        ("GET", "/job/demo/job/gitlab/api/json"): (404, {}),
        ("POST", "/job/demo/createItem"): (200, {}),
    })


def fresh_sonar() -> FakeTransport:
    return FakeTransport({
        ("GET", "/api/user_groups/search"): (200, {"groups": []}),
        ("POST", "/api/user_groups/create"): (200, {}),
        ("GET", "/api/permissions/search_templates"): (200, {"permissionTemplates": []}),
        ("POST", "/api/permissions/create_template"): (200, {"permissionTemplate": {"id": "tpl1"}}),
        ("POST", "/api/permissions/add_group_to_template"): (204, {}),
        ("GET", "/api/user_tokens/search"): (200, {"userTokens": []}),
        ("POST", "/api/user_tokens/generate"): (200, {"token": "sqa_token_value"}),
        ("GET", "/api/user_groups/users"): (200, {"users": []}),
        ("GET", "/api/users/search"): (200, {"users": [{"login": "alice"}]}),
        ("POST", "/api/user_groups/add_user"): (204, {}),
    })


def lab_with(gitlab, jenkins, sonar) -> mock.Mock:
    lab = mock.Mock()
    lab.gitlab.return_value = gitlab
    lab.jenkins.return_value = jenkins
    lab.sonar.return_value = sonar
    return lab


class ValidationTests(unittest.TestCase):
    def test_team_names(self) -> None:
        self.assertEqual("demo-team", teams.valid_team("demo-team"))
        for bad in ("Demo", "1team", "a", "team_x", "x" * 32, "team/x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                teams.valid_team(bad)


class XmlTests(unittest.TestCase):
    def test_credential_xml_escapes_values(self) -> None:
        xml = teams.string_credential_xml("sonar-demo", "Sonar <token>", "a&b")
        self.assertIn("<id>sonar-demo</id>", xml)
        self.assertIn("Sonar &lt;token&gt;", xml)
        self.assertIn("<secret>a&amp;b</secret>", xml)
        token_xml = teams.gitlab_token_xml("gitlab-demo", "d", "t<")
        self.assertIn("PersonalAccessTokenImpl", token_xml)
        self.assertIn("<token>t&lt;</token>", token_xml)

    def test_organization_folder_xml_targets_team_group_and_credential(self) -> None:
        xml = teams.organization_folder_xml("demo", "gitlab-demo")
        self.assertIn("<serverName>platform</serverName>", xml)
        self.assertIn("<projectOwner>demo</projectOwner>", xml)
        self.assertIn("<credentialsId>gitlab-demo</credentialsId>", xml)
        self.assertIn("OrganizationFolder", xml)
        self.assertIn("<navigatorProjects/>", xml)


class AddTeamTests(unittest.TestCase):
    def test_fresh_team_creates_everything_and_binds_group_sids(self) -> None:
        gitlab, jenkins, sonar = fresh_gitlab(), fresh_jenkins(), fresh_sonar()
        report = teams.add_team(lab_with(gitlab, jenkins, sonar), "demo", members=["alice"], today=TODAY)

        self.assertEqual("created", report["gitlab_group"])
        self.assertEqual("created", report["jenkins_folder"])
        self.assertEqual("created", report["gitlab_token"])
        self.assertEqual("created", report["sonar_token"])
        posted = {(method, path) for method, path, _ in gitlab.posts()}
        self.assertEqual({("POST", "/groups"), ("POST", "/groups/7/access_tokens"), ("POST", "/groups/7/members")}, posted)
        token_fields = next(f for m, p, f in gitlab.posts() if p == "/groups/7/access_tokens")
        self.assertEqual({"name": "jenkins", "scopes[]": ["api", "read_repository"], "access_level": 40,
                          "expires_at": "2027-09-21"}, token_fields)
        roles = [f for m, p, f in jenkins.posts() if p == "/role-strategy/strategy/addRole"]
        self.assertEqual({"projectRoles", "slaveRoles"}, {r["type"] for r in roles})
        project = next(r for r in roles if r["type"] == "projectRoles")
        self.assertEqual("^demo(/.*)?$", project["pattern"])
        self.assertIn("hudson.model.Item.Build", project["permissionIds"].split(","))
        self.assertIn("com.cloudbees.plugins.credentials.CredentialsProvider.View", project["permissionIds"].split(","))
        node = next(r for r in roles if r["type"] == "slaveRoles")
        self.assertEqual("^demo-.*$", node["pattern"])
        groups = [f for m, p, f in jenkins.posts() if p == "/role-strategy/strategy/assignGroupRole"]
        self.assertEqual([{"type": "projectRoles", "roleName": "demo", "group": "demo"},
                          {"type": "slaveRoles", "roleName": "demo", "group": "demo"}], groups)
        users = [f for m, p, f in jenkins.posts() if p == "/role-strategy/strategy/assignUserRole"]
        self.assertEqual({"alice"}, {u["user"] for u in users})
        created = [body for m, p, body in jenkins.posts() if p == "/job/demo/credentials/store/folder/domain/_/createCredentials"]
        self.assertEqual(2, len(created))
        self.assertIn("glgat-team-token-value", created[0])
        self.assertIn("sqa_token_value", created[1])
        org = next(body for m, p, body in jenkins.posts() if p == "/job/demo/createItem?name=gitlab")
        self.assertIn("<projectOwner>demo</projectOwner>", org)
        template = next(f for m, p, f in sonar.posts() if p == "/api/permissions/create_template")
        self.assertEqual({"name": "demo", "projectKeyPattern": "^demo[-_:].*"}, template)
        grants = [f["permission"] for m, p, f in sonar.posts() if p == "/api/permissions/add_group_to_template"]
        self.assertEqual(sorted(teams.SONAR_TEMPLATE_PERMISSIONS), sorted(grants))
        generated = next(f for m, p, f in sonar.posts() if p == "/api/user_tokens/generate")
        self.assertEqual({"name": "jenkins-demo", "type": "GLOBAL_ANALYSIS_TOKEN"}, generated)
        self.assertIn(("POST", "/api/user_groups/add_user", {"name": "demo", "login": "alice"}), sonar.posts())

    def test_rerun_with_everything_present_makes_no_changes(self) -> None:
        gitlab = FakeTransport({
            ("GET", "/groups"): (200, [{"id": 7, "full_path": "demo"}]),
            ("GET", "/groups/7/access_tokens"): (200, [{"id": 1, "name": "jenkins", "active": True, "revoked": False}]),
        })
        role = {"permissionIds": {p: True for p in teams.ITEM_PERMISSIONS}, "sids": ["demo"]}
        node_role = {"permissionIds": {p: True for p in teams.NODE_PERMISSIONS}, "sids": ["demo"]}
        jenkins = FakeTransport({
            ("GET", "/job/demo/api/json"): (200, {}),
            ("GET", "/role-strategy/strategy/getRole"): [(200, role), (200, node_role)],
            ("GET", "/job/demo/credentials/store/folder/domain/_/credential/gitlab-demo/api/json"): (200, {}),
            ("GET", "/job/demo/credentials/store/folder/domain/_/credential/sonar-demo/api/json"): (200, {}),
            ("GET", "/job/demo/job/gitlab/api/json"): (200, {}),
            ("POST", "/role-strategy/strategy/assignGroupRole"): (200, {}),
        })
        sonar = FakeTransport({
            ("GET", "/api/user_groups/search"): (200, {"groups": [{"name": "demo"}]}),
            ("GET", "/api/permissions/search_templates"): (200, {"permissionTemplates": [{"id": "tpl1", "name": "demo", "projectKeyPattern": "^demo[-_:].*"}]}),
            ("POST", "/api/permissions/add_group_to_template"): (204, {}),
            ("GET", "/api/user_tokens/search"): (200, {"userTokens": [{"name": "jenkins-demo"}]}),
        })
        report = teams.add_team(lab_with(gitlab, jenkins, sonar), "demo", today=TODAY)
        self.assertEqual("kept", report["gitlab_token"])
        self.assertEqual("kept", report["sonar_token"])
        self.assertEqual([], gitlab.posts())
        self.assertEqual({"/role-strategy/strategy/assignGroupRole"}, {p for _, p, _ in jenkins.posts()})
        self.assertEqual({"/api/permissions/add_group_to_template"}, {p for _, p, _ in sonar.posts()})

    def test_missing_jenkins_credential_rotates_the_matching_token(self) -> None:
        gitlab = FakeTransport({
            ("GET", "/groups"): (200, [{"id": 7, "full_path": "demo"}]),
            ("GET", "/groups/7/access_tokens"): (200, [{"id": 1, "name": "jenkins", "active": True, "revoked": False}]),
            ("DELETE", "/groups/7/access_tokens/1"): (204, {}),
            ("POST", "/groups/7/access_tokens"): (201, {"id": 2, "token": "glgat-rotated"}),
        })
        jenkins = fresh_jenkins()
        jenkins.responses[("GET", "/job/demo/api/json")] = [(200, {})]
        jenkins.responses[("GET", "/job/demo/credentials/store/folder/domain/_/credential/sonar-demo/api/json")] = [(200, {})]
        jenkins.responses[("GET", "/job/demo/job/gitlab/api/json")] = [(200, {})]
        sonar = fresh_sonar()
        sonar.responses[("GET", "/api/user_tokens/search")] = [(200, {"userTokens": [{"name": "jenkins-demo"}]})]
        report = teams.add_team(lab_with(gitlab, jenkins, sonar), "demo", today=TODAY)
        self.assertEqual("rotated", report["gitlab_token"])
        self.assertEqual("kept", report["sonar_token"])
        self.assertIn(("DELETE", "/groups/7/access_tokens/1", None), gitlab.posts())
        created = [body for m, p, body in jenkins.posts() if "createCredentials" in p]
        self.assertEqual(1, len(created))
        self.assertIn("glgat-rotated", created[0])

    def test_rotate_flag_replaces_both_tokens_and_updates_existing_credentials(self) -> None:
        gitlab = FakeTransport({
            ("GET", "/groups"): (200, [{"id": 7, "full_path": "demo"}]),
            ("GET", "/groups/7/access_tokens"): (200, [{"id": 1, "name": "jenkins", "active": True, "revoked": False}]),
            ("DELETE", "/groups/7/access_tokens/1"): (204, {}),
            ("POST", "/groups/7/access_tokens"): (201, {"id": 2, "token": "glgat-rotated"}),
        })
        jenkins = FakeTransport({
            ("GET", "/job/demo/api/json"): (200, {}),
            ("GET", "/role-strategy/strategy/getRole"): (200, {"permissionIds": {}, "sids": []}),
            ("POST", "/role-strategy/strategy/addRole"): (200, {}),
            ("POST", "/role-strategy/strategy/assignGroupRole"): (200, {}),
            ("GET", "/job/demo/credentials/store/folder/domain/_/credential/gitlab-demo/api/json"): (200, {}),
            ("GET", "/job/demo/credentials/store/folder/domain/_/credential/sonar-demo/api/json"): (200, {}),
            ("POST", "/job/demo/credentials/store/folder/domain/_/credential/gitlab-demo/config.xml"): (200, {}),
            ("POST", "/job/demo/credentials/store/folder/domain/_/credential/sonar-demo/config.xml"): (200, {}),
            ("GET", "/job/demo/job/gitlab/api/json"): (200, {}),
        })
        sonar = fresh_sonar()
        sonar.responses[("GET", "/api/user_tokens/search")] = [(200, {"userTokens": [{"name": "jenkins-demo"}]})]
        sonar.responses[("POST", "/api/user_tokens/revoke")] = [(204, {})]
        report = teams.add_team(lab_with(gitlab, jenkins, sonar), "demo", rotate_tokens=True, today=TODAY)
        self.assertEqual(("rotated", "rotated"), (report["gitlab_token"], report["sonar_token"]))
        updates = [p for m, p, _ in jenkins.posts() if p.endswith("/config.xml")]
        self.assertEqual(2, len(updates))
        self.assertIn(("POST", "/api/user_tokens/revoke", {"name": "jenkins-demo", "login": "admin"}), sonar.posts())



class SonarMemberTests(unittest.TestCase):
    def transport(self, in_group, known):
        return FakeTransport({
            ("GET", "/api/user_groups/users"): (200, {"users": [{"login": "alice"}] if in_group else []}),
            ("GET", "/api/users/search"): (200, {"users": [{"login": "alice"}] if known else []}),
            ("POST", "/api/user_groups/add_user"): (204, {}),
        })

    def test_existing_member_is_kept(self) -> None:
        sonar = self.transport(in_group=True, known=True)
        self.assertEqual("kept", teams.ensure_sonar_member(sonar, "demo", "alice"))
        self.assertEqual([], sonar.posts())

    def test_known_user_is_added(self) -> None:
        sonar = self.transport(in_group=False, known=True)
        self.assertEqual("added", teams.ensure_sonar_member(sonar, "demo", "alice"))
        self.assertEqual([("POST", "/api/user_groups/add_user", {"name": "demo", "login": "alice"})], sonar.posts())

    def test_user_who_has_never_logged_in_is_left_to_group_sync(self) -> None:
        """SonarQube answers HTTP 404 for a login it does not hold yet."""
        sonar = self.transport(in_group=False, known=False)
        self.assertEqual("pending-first-login", teams.ensure_sonar_member(sonar, "demo", "alice"))
        self.assertEqual([], sonar.posts())

if __name__ == "__main__":
    unittest.main()
