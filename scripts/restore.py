#!/usr/bin/env python3
"""Restore a backup set into a new runtime root, for real or as an isolated rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backup import CleanupError, attempt_cleanup, installed_plugins  # noqa: E402
from backupset import IMAGES, extract_tar, identity_from_manifest, validate_destination, validate_tar, verify_backup, write_json  # noqa: E402
from bootstrap import parser as setup_parser, setup  # noqa: E402
from compose import PINS_FILE, SERVICES, Lab, load_versions, run  # noqa: E402
from platform_init import initialise  # noqa: E402
from status import endpoint  # noqa: E402

APPLICATION_SERVICES = ("gitlab", "jenkins", "postgres", "sonarqube")
PROBE_SCRIPT = (
    "command -v timeout >/dev/null || exit 99\n"
    "if timeout 3 bash -c 'exec 3<>/dev/tcp/$1/$2' bash \"$1\" \"$2\" 2>/dev/null; "
    "then echo REACHABLE; else echo BLOCKED; fi"
)


def assert_port_free(address: str, port: int) -> None:
    """Probe by connecting, not by binding.

    Docker publishes the port as root, so binding it from this unprivileged process
    would fail on a privileged port that is in fact free. A successful connect is the
    only evidence that something is already listening.
    """
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex((address, port)) == 0:
            raise ValueError(f"port {port} is already in use on {address}")


def parse_arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--backup", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--base-domain")
    parser.add_argument("--https-port")
    parser.add_argument("--ssh-port")
    parser.add_argument("--bind-address")
    parser.add_argument("--rehearsal", action="store_true",
                        help="isolated restore under a different identity; the clone stack is "
                             "removed afterwards, the target root stays for inspection")
    return parser.parse_args(argv)


def resolve_identity(manifest: dict, arguments: argparse.Namespace) -> dict[str, str]:
    identity = identity_from_manifest(manifest)
    for key in identity:
        value = getattr(arguments, key)
        if value is not None:
            identity[key] = str(value)
    return identity


def ensure_images(lab: Lab, manifest: dict, backup: pathlib.Path) -> str:
    if manifest.get("includes_images"):
        print("Loading the archived images...", flush=True)
        run(["docker", "load", "-i", backup / IMAGES], timeout=3600)
        for service, image in manifest["images"].items():
            actual = run(["docker", "image", "inspect", image["id"], "--format", "{{.Id}}"]).decode().strip()
            if actual != image["id"]:
                raise RuntimeError(f"archived image for {service} did not load with its recorded id")
        return "loaded"
    print("Pulling the pinned upstream images...", flush=True)
    for service in ("gitlab", "postgres", "sonarqube"):
        run(["docker", "pull", "--quiet", lab.versions[service]["reference"]], timeout=3600)
    return "pulled"


def restore_overlay(lab: Lab, manifest: dict, *, rehearsal: bool) -> pathlib.Path | None:
    """The rehearsal-only Compose overlay: internal networks, loopback publish, source hosts fenced."""
    if not rehearsal:
        return None
    source = manifest["source"]
    hosts = {source[key]: "127.0.0.1" for key in ("GITLAB_HOST", "JENKINS_HOST", "SONAR_HOST")}
    services: dict[str, dict] = {service: {"extra_hosts": dict(hosts)} for service in SERVICES}
    services["edge"].update({
        "networks": ["edge", "recovery-front"],
        "ports": [f"127.0.0.1:{lab.env['HTTPS_PORT']}:{lab.env['HTTPS_PORT']}"],
    })
    services["gitlab"]["ports"] = [f"127.0.0.1:{lab.env['GITLAB_SSH_PORT']}:22"]
    overlay = {
        "networks": {
            "edge": {"internal": True},
            "sonar-db": {"internal": True},
            "recovery-front": {"name": lab.env["COMPOSE_PROJECT_NAME"] + "_recovery-front", "internal": False},
        },
        "services": services,
    }
    path = lab.root / "config" / "recovery-compose.json"
    write_json(path, overlay)
    return path


def write_image_pins(lab: Lab, manifest: dict) -> pathlib.Path:
    """Pin every service to the image id `docker load` restored, permanently for this root.

    A loaded image carries no repository name, so the `name:tag@sha256:` references in
    versions.env would send Compose to a registry; and Compose builds only a service whose
    image is missing, so the pins also keep `edge` and `jenkins` from rebuilding. The file is
    picked up automatically by compose_command(), which is why `scripts/lab up` after an
    air-gapped restore neither pulls nor builds. Delete it to return to versions.env.
    """
    pins = {"services": {service: {"image": image["id"], "pull_policy": "never"}
                         for service, image in manifest["images"].items()}}
    path = lab.root / "config" / PINS_FILE
    write_json(path, pins)
    return path


def helper_image(lab: Lab, manifest: dict) -> str:
    """The image for one-off root helpers: the set's own Jenkins image when it carries images."""
    if manifest.get("includes_images"):
        return manifest["images"]["jenkins"]["id"]
    return lab.versions["jenkins"]["reference"]


def fence(lab: Lab, source: dict) -> dict[str, bool]:
    """Prove the rehearsal clone cannot reach the source platform or the internet."""
    project = lab.env["COMPOSE_PROJECT_NAME"]
    names = [f"{project}_{name}" for name in ("edge", "sonar-db", "recovery-front")]
    networks = json.loads(run(["docker", "network", "inspect", *names]))
    if not all(network["Internal"] for network in networks[:2]) or networks[2]["Internal"]:
        raise RuntimeError("unexpected frontend/backend isolation topology")
    front_members = []
    for container in lab.dc("ps", "-q").decode().split():
        info = json.loads(run(["docker", "inspect", container]))[0]
        service = info["Config"]["Labels"]["com.docker.compose.service"]
        attached = set(info["NetworkSettings"]["Networks"])
        if names[2] in attached:
            front_members.append(service)
        allowed = set(names if service == "edge" else names[:2])
        if attached - allowed:
            raise RuntimeError("unexpected attached network")
        if service == "edge":
            published = info["HostConfig"]["PortBindings"][lab.env["HTTPS_PORT"] + "/tcp"]
            if any(binding["HostIp"] != "127.0.0.1" for binding in published):
                raise RuntimeError("frontend must bind loopback")
    if front_members != ["edge"]:
        raise RuntimeError("frontend must attach only the edge")
    for service in APPLICATION_SERVICES:
        for url in (source["GITLAB_URL"], source["JENKINS_URL"], source["SONAR_URL"], "https://1.1.1.1"):
            parsed = urllib.parse.urlsplit(url)
            result = lab.exec(service, "bash", "-c", PROBE_SCRIPT, "bash", parsed.hostname, str(parsed.port or 443)).decode().strip()
            if result != "BLOCKED":
                raise RuntimeError(f"{service} can reach {parsed.hostname}; the rehearsal is not fenced")
    return {"application_networks_internal": True, "edge_only_frontend": True,
            "https_bind_loopback": True, "source_and_internet_blocked": True}


def wait_gitlab_ready(lab: Lab, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lab.exec("gitlab", "bash", "-c",
                     'curl --noproxy "*" --fail --silent --max-time 10 "http://127.0.0.1/-/readiness?all=1" >/dev/null '
                     '&& ! pgrep -f "[c]inc-client" >/dev/null')
            return
        except RuntimeError:
            time.sleep(5)
    raise RuntimeError("GitLab did not complete initial configuration and application readiness")


def wait_healthy(lab: Lab, services) -> None:
    lab.dc("up", "-d", "--wait", "--wait-timeout", "900", *services, timeout=1000)


def place_gitlab_config(lab: Lab, config_tar: pathlib.Path, image: str) -> None:
    """Extract /etc/gitlab (root-owned) with a one-off root helper.

    The archive was validated by restore() before any mutation; do not reopen it here.
    """
    target = lab.root / "data" / "gitlab" / "config"
    run(["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh",
         "-v", f"{config_tar}:/source/config.tar:ro", "-v", f"{target}:/target",
         image, "-eu", "-c",
         "tar --numeric-owner -xf /source/config.tar -C /target"], timeout=600)


def teardown(lab: Lab, *, original: BaseException | None = None) -> None:
    attempt_cleanup([("remove rehearsal stack", lambda: lab.dc("down", "--volumes", timeout=300))],
                    stage="rehearsal teardown", original=original)


def restore(arguments: argparse.Namespace) -> pathlib.Path:
    started = time.monotonic()
    os.umask(0o077)
    repo = arguments.repo.resolve()
    backup = arguments.backup.resolve()
    versions = load_versions(repo)
    manifest = verify_backup(backup, versions)
    identity = resolve_identity(manifest, arguments)
    if arguments.rehearsal:
        # Compose appends `ports` across files instead of replacing them, so the overlay cannot
        # narrow a 0.0.0.0 publish coming from compose.yaml; the identity itself must be loopback.
        # With this set, the overlay's entries are identical to the base ones and Compose dedupes them.
        if arguments.bind_address not in (None, "127.0.0.1"):
            raise ValueError("a rehearsal must bind 127.0.0.1")
        identity["bind_address"] = "127.0.0.1"
    validate_destination(manifest, arguments.root, identity["project"], identity["base_domain"],
                         int(identity["https_port"]), int(identity["ssh_port"]), rehearsal=arguments.rehearsal)
    if run(["git", "-C", repo, "rev-parse", "HEAD"]).decode().strip() != manifest["revision"]:
        raise ValueError("checkout must match the backup's code revision")
    if run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"]).strip():
        raise ValueError("checkout must be clean")
    for name in ("gitlab/config.tar", "jenkins/home.tar", "runtime/secrets.tar", "runtime/tls.tar", "runtime/config.tar"):
        validate_tar(backup / name)
    probe_address = identity["bind_address"] if identity["bind_address"] != "0.0.0.0" else "127.0.0.1"
    for port in (int(identity["https_port"]), int(identity["ssh_port"])):
        assert_port_free(probe_address, port)
    if run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + identity["project"]]).strip():
        raise ValueError("a Compose project with the destination name already exists")

    root = arguments.root.resolve()
    setup_flags = ["--root", str(root), "--project", identity["project"], "--base-domain", identity["base_domain"],
                   "--https-port", identity["https_port"], "--ssh-port", identity["ssh_port"],
                   "--bind-address", identity["bind_address"]]
    setup(setup_parser().parse_args(setup_flags))
    extract_tar(backup / "runtime/secrets.tar", root / "secrets")
    extract_tar(backup / "runtime/tls.tar", root / "tls")
    extract_tar(backup / "runtime/config.tar", root / "config",
                exclude=("runtime.env", "recovery-compose.json", "recovery-images.json", PINS_FILE))
    setup(setup_parser().parse_args(setup_flags))
    lab = Lab(repo, root)
    ensure_images(lab, manifest, backup)
    image = helper_image(lab, manifest)
    extract_tar(backup / "jenkins/home.tar", root / "data" / "jenkins")
    place_gitlab_config(lab, backup / "gitlab/config.tar", image)
    run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "sh",
         "-v", f"{root / 'tls'}:/output", image, "-eu", "-c",
         'cp "$JAVA_HOME/lib/security/cacerts" /output/trust/java-cacerts.p12; '
         "keytool -importcert -noprompt -alias devops-lab-ca -file /output/ca.crt "
         "-keystore /output/trust/java-cacerts.p12 -storepass changeit >/dev/null"])
    (root / "tls" / "trust" / "java-cacerts.p12").chmod(0o600)
    pin_images = bool(manifest.get("includes_images"))
    if pin_images:
        write_image_pins(lab, manifest)
    lab.overlay = restore_overlay(lab, manifest, rehearsal=arguments.rehearsal)
    evidence = {"mode": "rehearsal" if arguments.rehearsal else "restore", "revision": manifest["revision"],
                "versions": manifest["versions"], "backup": str(backup)}
    backup_id = manifest["gitlab_backup_id"]
    try:
        print("Starting the database and GitLab...", flush=True)
        if not pin_images:
            lab.dc("build", "edge", "jenkins", timeout=1800)
        wait_healthy(lab, ["postgres", "gitlab"])
        wait_gitlab_ready(lab)
        lab.dc("cp", str(backup / "gitlab/application.tar"), f"gitlab:/var/opt/gitlab/backups/{backup_id}_gitlab_backup.tar")
        lab.exec("gitlab", "chown", "git:git", f"/var/opt/gitlab/backups/{backup_id}_gitlab_backup.tar")
        lab.exec("gitlab", "gitlab-ctl", "stop", "puma")
        lab.exec("gitlab", "gitlab-ctl", "stop", "sidekiq")
        print("Restoring GitLab and the SonarQube database...", flush=True)
        lab.exec("gitlab", "gitlab-backup", "restore", f"BACKUP={backup_id}", "force=yes", timeout=3600)
        if arguments.rehearsal:
            lab.exec("gitlab", "gitlab-rails", "runner",
                     "WebHook.delete_all; Ci::Runner.update_all(active: false); puts 'fenced'", timeout=300)
        lab.exec("gitlab", "gitlab-ctl", "start", "puma")
        lab.exec("gitlab", "gitlab-ctl", "start", "sidekiq")
        lab.exec("postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "sonar", "-d", "postgres",
                 "-c", "DROP DATABASE sonar WITH (FORCE)",
                 "-c", "CREATE DATABASE sonar OWNER sonar TEMPLATE template0 ENCODING 'UTF8'")
        lab.exec("postgres", "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
                 "-U", "sonar", "-d", "sonar", input=(backup / "sonar/sonar.dump").read_bytes())
        print("Starting every service...", flush=True)
        wait_healthy(lab, [])
        wait_gitlab_ready(lab)
        if arguments.rehearsal:
            evidence["fence"] = fence(lab, manifest["source"])
        for key, path, expected in (("GITLAB", "/users/sign_in", ("200",)), ("JENKINS", "/login", ("200", "302")),
                                    ("SONAR", "/api/system/status", ("200",))):
            ok, detail = endpoint(root, lab.env[key + "_HOST"], lab.env["HTTPS_PORT"], path, lab.resolve_address, expected)
            if not ok:
                raise RuntimeError(f"{key} endpoint check failed: {detail}")
        evidence["endpoints"] = True
        if installed_plugins(lab) != manifest["plugins"]:
            raise RuntimeError("restored Jenkins plugin versions differ from the backup")
        evidence["plugins_exact"] = True
        lab.groovy('jenkins.model.Jenkins.instance.doCancelQuietDown(); println "READY"')
        if not arguments.rehearsal:
            evidence["platform_init"] = initialise(lab)
        evidence["elapsed_seconds"] = round(time.monotonic() - started)
        write_json(root / "evidence" / "restore.json", evidence)
        print(f"Restore verified; evidence at {root / 'evidence' / 'restore.json'}", flush=True)
    finally:
        if arguments.rehearsal:
            teardown(lab, original=sys.exception())
    return root


def main() -> int:
    try:
        root = restore(parse_arguments())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"restore failed: {error}", file=sys.stderr)
        return 1
    print(f"Platform restored at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
