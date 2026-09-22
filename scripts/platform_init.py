#!/usr/bin/env python3
"""Idempotent initialisation that runs after every `up` and after a real restore."""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bootstrap import atomic_write  # noqa: E402
from compose import Lab  # noqa: E402
from transport import basic_auth_headers, validate_https_origin  # noqa: E402

ROOT_TOKEN_NAME = "devops-lab-platform"
TOKEN_LIFETIME_DAYS = 365
TOKEN_RENEWAL_DAYS = 30
SONAR_WEBHOOK_NAME = "jenkins"
# GitLab 19 appends a routable suffix, so a token reads `glpat-<secret>.<version>.<routing>`.
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_.-]{20,}$")


def sonar_valid(client) -> bool:
    status, payload, _ = client.request("GET", "/api/authentication/validate", expected=(200, 401))
    return status == 200 and bool(payload.get("valid"))


def ensure_sonar_admin_password(lab: Lab) -> str:
    """Move SonarQube off the default admin/admin password to the stored secret."""
    if sonar_valid(lab.sonar()):
        return "kept"
    initial = lab.transport(lab.env["SONAR_URL"], basic_auth_headers("admin", "admin"))
    if not sonar_valid(initial):
        raise RuntimeError("SonarQube admin credentials are neither the stored secret nor the default; "
                           "reset them in the SonarQube UI and write the new value to secrets/sonar_admin_password")
    initial.request(
        "POST", "/api/users/change_password",
        {"login": "admin", "previousPassword": "admin", "password": lab.secret("sonar_admin_password")},
        expected=(204,),
    )
    if not sonar_valid(lab.sonar()):
        raise RuntimeError("SonarQube did not accept the stored admin password after rotation")
    return "rotated"


def ensure_sonar_setting(client, key: str, value: str) -> str:
    _, payload, _ = client.request("GET", "/api/settings/values?keys=" + urllib.parse.quote(key))
    current = next((item.get("value") for item in payload.get("settings", []) if item.get("key") == key), None)
    if current == value:
        return "kept"
    client.request("POST", "/api/settings/set", {"key": key, "value": value}, expected=(204,))
    return "updated"


def ensure_sonar_webhook(client, name: str, url: str) -> str:
    _, payload, _ = client.request("GET", "/api/webhooks/list")
    existing = [hook for hook in payload.get("webhooks", []) if hook.get("name") == name]
    if existing and existing[0].get("url") == url:
        return "kept"
    if existing:
        client.request("POST", "/api/webhooks/update",
                       {"webhook": existing[0]["key"], "name": name, "url": url}, expected=(204,))
        return "updated"
    client.request("POST", "/api/webhooks/create", {"name": name, "url": url})
    return "created"


def token_needs_renewal(expires_at: str | None, today: datetime.date) -> bool:
    if not expires_at:
        return True
    expiry = datetime.date.fromisoformat(expires_at[:10])
    return (expiry - today).days < TOKEN_RENEWAL_DAYS


def ensure_gitlab_root_token(lab: Lab, today: datetime.date | None = None) -> str:
    """Keep one root API token on disk, recreated through gitlab-rails when missing or expiring."""
    today = today or datetime.date.today()
    token_file = lab.root / "secrets" / "gitlab_root_token"
    if token_file.exists():
        status, payload, _ = lab.gitlab().request("GET", "/personal_access_tokens/self", expected=(200, 401))
        if status == 200 and not token_needs_renewal(payload.get("expires_at"), today):
            return "kept"
    runner = (
        "user = User.find_by_username('root')\n"
        f"user.personal_access_tokens.where(name: '{ROOT_TOKEN_NAME}').each(&:revoke!)\n"
        f"token = user.personal_access_tokens.create!(name: '{ROOT_TOKEN_NAME}', scopes: ['api'], "
        f"expires_at: {TOKEN_LIFETIME_DAYS}.days.from_now)\n"
        "puts token.token\n"
    )
    output = lab.exec("gitlab", "gitlab-rails", "runner", runner, timeout=300).decode(errors="replace")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or not TOKEN_SHAPE.match(lines[-1]):
        raise RuntimeError("GitLab did not return a usable root token")
    atomic_write(token_file, lines[-1], 0o600)
    return "created"


def ensure_gitlab_outbound_allowlist(lab: Lab, client) -> str:
    """Let GitLab webhooks reach the Jenkins host, which it otherwise treats as a blocked local address."""
    validate_https_origin(lab.env["JENKINS_URL"], lab.env["JENKINS_HOST"])
    target = urllib.parse.urlsplit(lab.env["JENKINS_URL"])
    entry = f"{target.hostname}:{target.port or 443}"
    _, settings, _ = client.request("GET", "/application/settings")
    allowed = list(settings.get("outbound_local_requests_whitelist", []))
    if entry in allowed:
        return "kept"
    client.request("PUT", "/application/settings", {"outbound_local_requests_whitelist[]": [*allowed, entry]})
    _, confirmed, _ = client.request("GET", "/application/settings")
    if entry not in confirmed.get("outbound_local_requests_whitelist", []):
        raise RuntimeError("GitLab did not confirm the Jenkins webhook allowlist entry")
    return "updated"


def initialise(lab: Lab) -> dict[str, str]:
    report = {"sonar_admin_password": ensure_sonar_admin_password(lab)}
    sonar = lab.sonar()
    report["sonar_base_url"] = ensure_sonar_setting(sonar, "sonar.core.serverBaseURL", lab.env["SONAR_URL"])
    report["sonar_webhook"] = ensure_sonar_webhook(sonar, SONAR_WEBHOOK_NAME, lab.env["JENKINS_URL"] + "/sonarqube-webhook/")
    report["gitlab_root_token"] = ensure_gitlab_root_token(lab)
    report["gitlab_outbound_allowlist"] = ensure_gitlab_outbound_allowlist(lab, lab.gitlab())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    arguments = parser.parse_args()
    try:
        for step, outcome in initialise(Lab(arguments.repo, arguments.root)).items():
            print(f"{step}: {outcome}")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"platform initialisation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
