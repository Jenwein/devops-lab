#!/usr/bin/env python3
"""Enable GitLab as the identity provider for Jenkins and SonarQube."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bootstrap import atomic_write  # noqa: E402
from compose import Lab  # noqa: E402

SONAR_APP_NAME = "devops-lab-sonarqube"
JENKINS_APP_NAME = "devops-lab-jenkins"
OVERLAY_NAME = "50-gitlab-auth.yaml"
SONAR_GITLAB_PATH = "/api/v2/dop-translation/gitlab-configurations"


def ensure_application(gitlab, name: str, redirect_uri: str, scopes: str, secret_file: pathlib.Path) -> tuple[str, str]:
    """Keep one confidential OAuth application whose secret we still hold; otherwise recreate it."""
    _, applications, _ = gitlab.request("GET", "/applications")
    existing = [app for app in applications if app.get("application_name") == name]
    if existing and secret_file.exists() and existing[0].get("callback_url") == redirect_uri:
        return existing[0]["application_id"], "kept"
    for app in existing:
        gitlab.request("DELETE", f"/applications/{app['id']}", expected=(204,))
    _, created, _ = gitlab.request(
        "POST", "/applications",
        {"name": name, "redirect_uri": redirect_uri, "scopes": scopes, "confidential": "true"},
        expected=(201,),
    )
    atomic_write(secret_file, created["secret"], 0o600)
    return created["application_id"], "created"


def configure_sonar_gitlab_auth(sonar, gitlab_url: str, application_id: str, secret: str,
                                *, application_changed: bool = True) -> str:
    """Configure GitLab authentication through the v2 endpoint.

    SonarQube 26 refuses `sonar.auth.gitlab.url` and the two secured keys through
    `api/settings/set` ("cannot be updated using this webservice. Please use the API
    v2"), so the whole configuration is written as one document here.
    """
    desired = {
        "enabled": True,
        "applicationId": application_id,
        "url": gitlab_url,
        "secret": secret,
        "synchronizeGroups": True,
        "allowUsersToSignUp": True,
        "provisioningType": "JIT",
    }
    _, found, _ = sonar.request("GET", SONAR_GITLAB_PATH)
    configurations = found.get("gitlabConfigurations", [])
    if not configurations:
        sonar.request("POST", SONAR_GITLAB_PATH, body=json.dumps({**desired, "allowedGroups": []}),
                      content_type="application/json", expected=(200, 201))
        return "created"
    current = configurations[0]
    readable = [key for key in desired if key != "secret"]
    # The secret cannot be read back, so a recreated application always forces a write.
    if not application_changed and all(current.get(key) == desired[key] for key in readable):
        return "kept"
    sonar.request("PATCH", f"{SONAR_GITLAB_PATH}/{current['id']}", body=json.dumps(desired),
                  content_type="application/merge-patch+json", expected=(200, 204))
    return "updated"


def render_jenkins_overlay(gitlab_url: str, client_id: str) -> str:
    """oic-auth realm against GitLab.

    The installed oic-auth (4.718.ve731df6ca_88a_) puts the escape hatch inside a
    `properties` list rather than as top-level `escapeHatch*` keys, and leaves
    `logoutFromOpenidProvider` at its default.
    """
    return (
        "# Written by scripts/lab enable-gitlab-auth. Delete this file and restart Jenkins to return to local login.\n"
        "jenkins:\n"
        "  securityRealm:\n"
        "    oic:\n"
        f"      clientId: \"{client_id}\"\n"
        "      clientSecret: \"${GITLAB_OIDC_CLIENT_SECRET}\"\n"
        "      serverConfiguration:\n"
        "        wellKnown:\n"
        f"          wellKnownOpenIDConfigurationUrl: \"{gitlab_url}/.well-known/openid-configuration\"\n"
        "          scopesOverride: \"openid profile email\"\n"
        "      userNameField: \"nickname\"\n"
        "      fullNameFieldName: \"name\"\n"
        "      emailFieldName: \"email\"\n"
        "      groupsFieldName: \"groups\"\n"
        "      properties:\n"
        "        - escapeHatch:\n"
        "            username: \"admin\"\n"
        "            secret: \"${JENKINS_ADMIN_PASSWORD}\"\n"
        "            group: \"admin\"\n"
    )


def wait_for_jenkins(lab: Lab, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            lab.transport(lab.env["JENKINS_URL"]).request("GET", "/login", expected=(200, 302))
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("Jenkins did not come back after enabling GitLab login")
            time.sleep(5)


def enable(lab: Lab) -> dict[str, str]:
    gitlab = lab.gitlab()
    report: dict[str, str] = {}
    sonar_secret = lab.root / "secrets" / "sonar_gitlab_oauth_secret"
    jenkins_secret = lab.root / "secrets" / "jenkins" / "gitlab_oidc_client_secret"
    sonar_app, report["sonar_application"] = ensure_application(
        gitlab, SONAR_APP_NAME, lab.env["SONAR_URL"] + "/oauth2/callback/gitlab", "api", sonar_secret)
    jenkins_app, report["jenkins_application"] = ensure_application(
        gitlab, JENKINS_APP_NAME, lab.env["JENKINS_URL"] + "/securityRealm/finishLogin", "openid profile email", jenkins_secret)
    report["sonar_settings"] = configure_sonar_gitlab_auth(
        lab.sonar(), lab.env["GITLAB_URL"], sonar_app, sonar_secret.read_text().strip(),
        application_changed=report["sonar_application"] != "kept")
    overlay = lab.root / "config" / "jenkins" / "casc.d" / OVERLAY_NAME
    content = render_jenkins_overlay(lab.env["GITLAB_URL"], jenkins_app)
    if overlay.exists() and overlay.read_text() == content and report["jenkins_application"] == "kept":
        report["jenkins"] = "kept"
        return report
    atomic_write(overlay, content, 0o600)
    lab.dc("restart", "jenkins", timeout=600)
    wait_for_jenkins(lab)
    report["jenkins"] = "restarted"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    arguments = parser.parse_args()
    try:
        for step, outcome in enable(Lab(arguments.repo, arguments.root)).items():
            print(f"{step}: {outcome}")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"enable-gitlab-auth failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
