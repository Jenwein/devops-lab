#!/usr/bin/env python3
"""Create or reconcile a team's space in GitLab, Jenkins and SonarQube."""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
import urllib.parse
from xml.sax.saxutils import escape

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bootstrap import atomic_write  # noqa: E402
from compose import Lab  # noqa: E402

TEAM_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
GITLAB_TOKEN_NAME = "jenkins"
GITLAB_TOKEN_ACCESS_LEVEL = 40
GITLAB_TOKEN_LIFETIME_DAYS = 365
GITLAB_MEMBER_ACCESS_LEVEL = 30
# Team roles are declared to Configuration as Code, so permissions use the JCasC
# names. Configuration as Code rebuilds the whole Role Strategy configuration from
# YAML on every Jenkins start; a role added through the REST API disappeared at the
# next restart, which is why the overlay below is the only durable home for them.
TEAMS_OVERLAY = "20-teams.yaml"
ITEM_PERMISSIONS = (
    "Job/Build", "Job/Cancel", "Job/Configure", "Job/Create", "Job/Delete", "Job/Discover",
    "Job/Move", "Job/Read", "Job/Workspace",
    "Run/Delete", "Run/Replay", "Run/Update",
    "SCM/Tag",
    "Credentials/Create", "Credentials/Delete", "Credentials/ManageDomains", "Credentials/Update",
    "Credentials/View",
    "View/Configure", "View/Create", "View/Delete", "View/Read",
)
# Agent/Create is deliberately omitted: the Role Strategy plugin checks it against
# Jenkins itself (there is no node yet to match a node role's pattern against), so
# a node role cannot gate creation by name. Node creation stays an admin action.
NODE_PERMISSIONS = ("Agent/Build", "Agent/Configure", "Agent/Connect", "Agent/Delete", "Agent/Disconnect")
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


def team_roles(name: str, members) -> dict:
    """The team's item and node roles in JCasC form, bound to the team group and its members."""
    entries = [{"group": name}] + [{"user": member} for member in sorted(set(members))]
    return {
        "items": [{"name": name, "pattern": f"^{name}(/.*)?$", "permissions": list(ITEM_PERMISSIONS), "entries": entries}],
        "agents": [{"name": name, "pattern": f"^{name}-.*$", "permissions": list(NODE_PERMISSIONS), "entries": list(entries)}],
    }


def overlay_path(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(root) / "config" / "jenkins" / "casc.d" / TEAMS_OVERLAY


def read_team_overlay(root: pathlib.Path) -> dict:
    """The roles currently declared for teams: {"items": [...], "agents": [...]} or {}."""
    path = overlay_path(root)
    if not path.exists():
        return {}
    document = json.loads(path.read_text())
    return document["jenkins"]["authorizationStrategy"]["roleBased"]["roles"]


def write_team_overlay(root: pathlib.Path, roles: dict) -> pathlib.Path:
    """Write the overlay as JSON, which YAML parses; the standard library has no YAML writer.

    JCasC merges the overlay directory with the override strategy: mappings merge by
    key, so `global` stays in the core file, while sequences are replaced whole, so
    this file must always carry every team's roles.
    """
    document = {"jenkins": {"authorizationStrategy": {"roleBased": {"roles": {
        "items": roles.get("items", []), "agents": roles.get("agents", [])}}}}}
    path = overlay_path(root)
    atomic_write(path, json.dumps(document, indent=2) + "\n", 0o600)
    return path


def merge_team_roles(existing: dict, team: dict) -> dict:
    """Replace the team's roles, keeping earlier member entries and every other team."""
    merged: dict = {}
    for kind in ("items", "agents"):
        new_role = team[kind][0]
        old_role = next((r for r in existing.get(kind, []) if r["name"] == new_role["name"]), None)
        if old_role is not None:
            users = sorted({e["user"] for e in old_role["entries"] + new_role["entries"] if "user" in e})
            new_role = dict(new_role, entries=[{"group": new_role["name"]}] + [{"user": u} for u in users])
        others = [r for r in existing.get(kind, []) if r["name"] != new_role["name"]]
        merged[kind] = sorted(others + [new_role], key=lambda r: r["name"])
    return merged


def ensure_team_roles(root: pathlib.Path, jenkins, name: str, members) -> str:
    """Declare the team's roles in the overlay and make Jenkins reload it when it changed."""
    existing = read_team_overlay(root)
    merged = merge_team_roles(existing, team_roles(name, members))
    if merged == {"items": existing.get("items", []), "agents": existing.get("agents", [])}:
        return "kept"
    had_team = any(r["name"] == name for r in existing.get("items", []))
    write_team_overlay(root, merged)
    jenkins.request("POST", "/configuration-as-code/reload", expected=(200, 302))
    return "updated" if had_team else "created"


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


def resolve_sonar_login(sonar, username: str) -> str | None:
    """The SonarQube login for a username, or None if the person has never signed in.

    A user who arrived through GitLab keeps the GitLab username as `externalIdentity`
    but gets a suffixed login (alice -> alice90205), so match either field.
    """
    _, known, _ = sonar.request("GET", "/api/users/search?q=" + urllib.parse.quote(username))
    for user in known.get("users", []):
        if username in (user.get("login"), user.get("externalIdentity")):
            return user["login"]
    return None


def ensure_sonar_member(sonar, group: str, username: str) -> str:
    """Put an existing SonarQube user in the team group.

    SonarQube only holds a user once they have logged in, so a member added before
    their first login cannot be placed in the group yet; GitLab group sync assigns
    it when they arrive. Adding an unknown login would fail with HTTP 404.
    """
    login = resolve_sonar_login(sonar, username)
    if login is None:
        return "pending-first-login"
    _, payload, _ = sonar.request(
        "GET", "/api/user_groups/users?name=" + urllib.parse.quote(group) + "&selected=selected&q=" + urllib.parse.quote(login))
    if any(user.get("login") == login for user in payload.get("users", [])):
        return "kept"
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
    report["jenkins_roles"] = ensure_team_roles(lab.root, jenkins, name, members)

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
