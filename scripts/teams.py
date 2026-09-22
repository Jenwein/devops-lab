#!/usr/bin/env python3
"""Create or reconcile a team's space in GitLab, Jenkins and SonarQube."""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys
import urllib.parse
from xml.sax.saxutils import escape

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from compose import Lab  # noqa: E402

TEAM_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
GITLAB_TOKEN_NAME = "jenkins"
GITLAB_TOKEN_ACCESS_LEVEL = 40
GITLAB_TOKEN_LIFETIME_DAYS = 365
GITLAB_MEMBER_ACCESS_LEVEL = 30
ITEM_PERMISSIONS = (
    "hudson.model.Item.Build", "hudson.model.Item.Cancel", "hudson.model.Item.Configure",
    "hudson.model.Item.Create", "hudson.model.Item.Delete", "hudson.model.Item.Discover",
    "hudson.model.Item.Move", "hudson.model.Item.Read", "hudson.model.Item.Workspace",
    "hudson.model.Run.Delete", "hudson.model.Run.Replay", "hudson.model.Run.Update",
    "hudson.scm.SCM.Tag",
    "com.cloudbees.plugins.credentials.CredentialsProvider.Create",
    "com.cloudbees.plugins.credentials.CredentialsProvider.Delete",
    "com.cloudbees.plugins.credentials.CredentialsProvider.ManageDomains",
    "com.cloudbees.plugins.credentials.CredentialsProvider.Update",
    "com.cloudbees.plugins.credentials.CredentialsProvider.View",
    "hudson.model.View.Configure", "hudson.model.View.Create", "hudson.model.View.Delete",
    "hudson.model.View.Read",
)
# hudson.model.Computer.Create is deliberately omitted: Spike e showed the
# Role Strategy plugin checks Agent/Create against Jenkins itself (there is
# no node instance yet to match a node role's pattern against), so a node
# role's pattern cannot gate creation by name. Node creation stays an
# admin-only action.
NODE_PERMISSIONS = (
    "hudson.model.Computer.Build", "hudson.model.Computer.Configure", "hudson.model.Computer.Connect",
    "hudson.model.Computer.Delete", "hudson.model.Computer.Disconnect",
)
SONAR_TEMPLATE_PERMISSIONS = ("user", "codeviewer", "issueadmin", "securityhotspotadmin", "scan", "admin")
FOLDER_XML = '<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder"/>'
CREDENTIAL_STORE = "/job/{folder}/credentials/store/folder/domain/_"


def valid_team(name: str) -> str:
    if not TEAM_PATTERN.match(name or ""):
        raise ValueError("team name must match ^[a-z][a-z0-9-]{1,30}$")
    return name


# ----- GitLab -----------------------------------------------------------------

def ensure_group(gitlab, name: str) -> tuple[dict, str]:
    _, groups, _ = gitlab.request("GET", "/groups?search=" + urllib.parse.quote(name))
    for group in groups:
        if group.get("full_path") == name:
            return group, "kept"
    _, created, _ = gitlab.request("POST", "/groups", {"name": name, "path": name, "visibility": "private"}, expected=(201,))
    return created, "created"


def ensure_group_token(gitlab, group_id: int, *, rotate: bool, today: datetime.date) -> tuple[str | None, str]:
    path = f"/groups/{group_id}/access_tokens"
    _, tokens, _ = gitlab.request("GET", path)
    active = [t for t in tokens if t.get("name") == GITLAB_TOKEN_NAME and t.get("active", True) and not t.get("revoked")]
    if active and not rotate:
        return None, "kept"
    for token in active:
        gitlab.request("DELETE", f"{path}/{token['id']}", expected=(204,))
    expires = (today + datetime.timedelta(days=GITLAB_TOKEN_LIFETIME_DAYS)).isoformat()
    _, created, _ = gitlab.request(
        "POST", path,
        {"name": GITLAB_TOKEN_NAME, "scopes[]": ["api", "read_repository"],
         "access_level": GITLAB_TOKEN_ACCESS_LEVEL, "expires_at": expires},
        expected=(201,),
    )
    return created["token"], "rotated" if active else "created"


def find_user(gitlab, username: str) -> dict:
    _, users, _ = gitlab.request("GET", "/users?username=" + urllib.parse.quote(username))
    for user in users:
        if user.get("username") == username:
            return user
    raise RuntimeError(f"GitLab user {username!r} does not exist; create it first")


def ensure_group_member(gitlab, group_id: int, user_id: int) -> None:
    status, _, _ = gitlab.request("GET", f"/groups/{group_id}/members/all/{user_id}", expected=(200, 404))
    if status == 404:
        gitlab.request("POST", f"/groups/{group_id}/members",
                       {"user_id": user_id, "access_level": GITLAB_MEMBER_ACCESS_LEVEL}, expected=(201,))


# ----- Jenkins ----------------------------------------------------------------

def item_exists(jenkins, path: str) -> bool:
    status, _, _ = jenkins.request("GET", f"{path}/api/json", expected=(200, 404))
    return status == 200


def ensure_folder(jenkins, name: str) -> str:
    if item_exists(jenkins, f"/job/{name}"):
        return "kept"
    jenkins.request("POST", f"/createItem?name={urllib.parse.quote(name)}", body=FOLDER_XML)
    return "created"


def ensure_role(jenkins, role_type: str, name: str, pattern: str, permissions: tuple[str, ...]) -> str:
    query = f"type={role_type}&roleName={urllib.parse.quote(name)}"
    _, payload, _ = jenkins.request("GET", f"/role-strategy/strategy/getRole?{query}", expected=(200, 404))
    current = payload.get("permissionIds") if isinstance(payload, dict) else None
    if current is not None and set(current) == set(permissions) and payload.get("pattern", pattern) == pattern:
        return "kept"
    jenkins.request("POST", "/role-strategy/strategy/addRole",
                    {"type": role_type, "roleName": name, "permissionIds": ",".join(permissions),
                     "overwrite": "true", "pattern": pattern})
    return "created" if current is None else "updated"


def assign_role(jenkins, role_type: str, name: str, sid: str, kind: str) -> None:
    endpoint = "assignGroupRole" if kind == "group" else "assignUserRole"
    jenkins.request("POST", f"/role-strategy/strategy/{endpoint}", {"type": role_type, "roleName": name, kind: sid})


def credential_exists(jenkins, folder: str, credential_id: str) -> bool:
    return item_exists(jenkins, CREDENTIAL_STORE.format(folder=folder) + f"/credential/{credential_id}")


def ensure_credential(jenkins, folder: str, credential_id: str, xml: str) -> str:
    store = CREDENTIAL_STORE.format(folder=folder)
    if credential_exists(jenkins, folder, credential_id):
        jenkins.request("POST", f"{store}/credential/{credential_id}/config.xml", body=xml)
        return "updated"
    jenkins.request("POST", f"{store}/createCredentials", body=xml)
    return "created"


def gitlab_token_xml(credential_id: str, description: str, token: str) -> str:
    return (
        "<io.jenkins.plugins.gitlabserverconfig.credentials.PersonalAccessTokenImpl>"
        f"<scope>GLOBAL</scope><id>{escape(credential_id)}</id>"
        f"<description>{escape(description)}</description><token>{escape(token)}</token>"
        "</io.jenkins.plugins.gitlabserverconfig.credentials.PersonalAccessTokenImpl>"
    )


def string_credential_xml(credential_id: str, description: str, secret: str) -> str:
    return (
        "<org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>"
        f"<scope>GLOBAL</scope><id>{escape(credential_id)}</id>"
        f"<description>{escape(description)}</description><secret>{escape(secret)}</secret>"
        "</org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>"
    )


def organization_folder_xml(team: str, credential_id: str) -> str:
    """GitLab Group organization folder.

    Spike b found that a hand-built createItem XML for GitLabSCMNavigator
    without a <navigatorProjects/> element deserializes with that field
    left null (XStream does not invoke the @DataBoundConstructor), and the
    org-folder scan then dies with a NullPointerException the first time it
    records a discovered project. Adding an empty <navigatorProjects/>
    element inside GitLabSCMNavigator, right after </traits>, makes XStream
    instantiate it as an empty HashSet instead.
    """
    return f"""<?xml version='1.1' encoding='UTF-8'?>
<jenkins.branch.OrganizationFolder plugin="branch-api">
  <description>Pipelines discovered in GitLab group {escape(team)}</description>
  <displayName>gitlab</displayName>
  <properties/>
  <folderViews class="jenkins.branch.OrganizationFolderViewHolder"><owner reference="../.."/></folderViews>
  <healthMetrics/>
  <icon class="jenkins.branch.MetadataActionFolderIcon"><owner class="jenkins.branch.OrganizationFolder" reference="../.."/></icon>
  <orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy">
    <pruneDeadBranches>true</pruneDeadBranches><daysToKeep>-1</daysToKeep><numToKeep>-1</numToKeep>
  </orphanedItemStrategy>
  <triggers>
    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger><spec>H H * * *</spec><interval>86400000</interval></com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>
  </triggers>
  <disabled>false</disabled>
  <navigators>
    <io.jenkins.plugins.gitlabbranchsource.GitLabSCMNavigator plugin="gitlab-branch-source">
      <serverName>platform</serverName>
      <projectOwner>{escape(team)}</projectOwner>
      <credentialsId>{escape(credential_id)}</credentialsId>
      <traits>
        <io.jenkins.plugins.gitlabbranchsource.BranchDiscoveryTrait><strategyId>1</strategyId></io.jenkins.plugins.gitlabbranchsource.BranchDiscoveryTrait>
        <io.jenkins.plugins.gitlabbranchsource.OriginMergeRequestDiscoveryTrait><strategyId>1</strategyId></io.jenkins.plugins.gitlabbranchsource.OriginMergeRequestDiscoveryTrait>
      </traits>
      <navigatorProjects/>
    </io.jenkins.plugins.gitlabbranchsource.GitLabSCMNavigator>
  </navigators>
  <projectFactories>
    <org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProjectFactory plugin="workflow-multibranch"><scriptPath>Jenkinsfile</scriptPath></org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProjectFactory>
  </projectFactories>
  <buildStrategies/>
  <strategy class="jenkins.branch.DefaultBranchPropertyStrategy"><properties class="empty-list"/></strategy>
</jenkins.branch.OrganizationFolder>
"""


def ensure_organization_folder(jenkins, team: str) -> str:
    if item_exists(jenkins, f"/job/{team}/job/gitlab"):
        return "kept"
    jenkins.request("POST", f"/job/{team}/createItem?name=gitlab", body=organization_folder_xml(team, f"gitlab-{team}"))
    return "created"


# ----- SonarQube --------------------------------------------------------------

def ensure_sonar_group(sonar, name: str) -> str:
    _, payload, _ = sonar.request("GET", "/api/user_groups/search?q=" + urllib.parse.quote(name))
    if any(group.get("name") == name for group in payload.get("groups", [])):
        return "kept"
    sonar.request("POST", "/api/user_groups/create", {"name": name})
    return "created"


def ensure_sonar_template(sonar, name: str, pattern: str) -> str:
    _, payload, _ = sonar.request("GET", "/api/permissions/search_templates?q=" + urllib.parse.quote(name))
    template = next((t for t in payload.get("permissionTemplates", []) if t.get("name") == name), None)
    outcome = "kept"
    if template is None:
        _, created, _ = sonar.request("POST", "/api/permissions/create_template", {"name": name, "projectKeyPattern": pattern})
        template = created["permissionTemplate"]
        outcome = "created"
    elif template.get("projectKeyPattern") != pattern:
        sonar.request("POST", "/api/permissions/update_template", {"id": template["id"], "projectKeyPattern": pattern})
        outcome = "updated"
    for permission in SONAR_TEMPLATE_PERMISSIONS:
        sonar.request("POST", "/api/permissions/add_group_to_template",
                      {"templateId": template["id"], "groupName": name, "permission": permission}, expected=(204,))
    return outcome


def ensure_sonar_analysis_token(sonar, name: str, *, rotate: bool) -> tuple[str | None, str]:
    _, payload, _ = sonar.request("GET", "/api/user_tokens/search?login=admin")
    exists = any(token.get("name") == name for token in payload.get("userTokens", []))
    if exists and not rotate:
        return None, "kept"
    if exists:
        sonar.request("POST", "/api/user_tokens/revoke", {"name": name, "login": "admin"}, expected=(204,))
    _, generated, _ = sonar.request("POST", "/api/user_tokens/generate", {"name": name, "type": "GLOBAL_ANALYSIS_TOKEN"})
    return generated["token"], "rotated" if exists else "created"


def ensure_sonar_member(sonar, group: str, login: str) -> str:
    """Put an existing SonarQube user in the team group.

    SonarQube only holds a user once they have logged in, so a member added before
    their first login cannot be placed in the group yet; GitLab group sync assigns
    it when they arrive. Adding an unknown login would fail with HTTP 404.
    """
    _, payload, _ = sonar.request(
        "GET", "/api/user_groups/users?name=" + urllib.parse.quote(group) + "&selected=selected&q=" + urllib.parse.quote(login))
    if any(user.get("login") == login for user in payload.get("users", [])):
        return "kept"
    _, known, _ = sonar.request("GET", "/api/users/search?q=" + urllib.parse.quote(login))
    if not any(user.get("login") == login for user in known.get("users", [])):
        return "pending-first-login"
    sonar.request("POST", "/api/user_groups/add_user", {"name": group, "login": login}, expected=(204,))
    return "added"


# ----- Orchestration ----------------------------------------------------------

def add_team(lab: Lab, name: str, members=(), rotate_tokens: bool = False, today: datetime.date | None = None) -> dict[str, str]:
    name = valid_team(name)
    today = today or datetime.date.today()
    gitlab, jenkins, sonar = lab.gitlab(), lab.jenkins(), lab.sonar()
    report: dict[str, str] = {}

    group, report["gitlab_group"] = ensure_group(gitlab, name)
    report["jenkins_folder"] = ensure_folder(jenkins, name)
    report["jenkins_item_role"] = ensure_role(jenkins, "projectRoles", name, f"^{name}(/.*)?$", ITEM_PERMISSIONS)
    report["jenkins_node_role"] = ensure_role(jenkins, "slaveRoles", name, f"^{name}-.*$", NODE_PERMISSIONS)
    assign_role(jenkins, "projectRoles", name, name, "group")
    assign_role(jenkins, "slaveRoles", name, name, "group")

    gitlab_credential = f"gitlab-{name}"
    token, report["gitlab_token"] = ensure_group_token(
        gitlab, int(group["id"]), rotate=rotate_tokens or not credential_exists(jenkins, name, gitlab_credential), today=today)
    if token is not None:
        report["jenkins_gitlab_credential"] = ensure_credential(
            jenkins, name, gitlab_credential, gitlab_token_xml(gitlab_credential, f"GitLab group token for {name}", token))
    report["jenkins_organization_folder"] = ensure_organization_folder(jenkins, name)

    report["sonar_group"] = ensure_sonar_group(sonar, name)
    report["sonar_template"] = ensure_sonar_template(sonar, name, f"^{name}[-_:].*")
    sonar_credential = f"sonar-{name}"
    analysis, report["sonar_token"] = ensure_sonar_analysis_token(
        sonar, f"jenkins-{name}", rotate=rotate_tokens or not credential_exists(jenkins, name, sonar_credential))
    if analysis is not None:
        report["jenkins_sonar_credential"] = ensure_credential(
            jenkins, name, sonar_credential, string_credential_xml(sonar_credential, f"SonarQube analysis token for {name}", analysis))

    sonar_members = []
    for member in members:
        user = find_user(gitlab, member)
        ensure_group_member(gitlab, int(group["id"]), int(user["id"]))
        assign_role(jenkins, "projectRoles", name, member, "user")
        assign_role(jenkins, "slaveRoles", name, member, "user")
        sonar_members.append(f"{member}={ensure_sonar_member(sonar, name, member)}")
    report["members"] = ",".join(members) or "none"
    report["sonar_members"] = ",".join(sonar_members) or "none"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("name")
    parser.add_argument("--member", action="append", default=[], help="existing username to add (repeatable)")
    parser.add_argument("--rotate-tokens", action="store_true")
    arguments = parser.parse_args()
    try:
        report = add_team(Lab(arguments.repo, arguments.root), arguments.name, arguments.member, arguments.rotate_tokens)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"add-team failed: {error}", file=sys.stderr)
        return 1
    for step, outcome in report.items():
        print(f"{step}: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
