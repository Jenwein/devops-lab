#!/usr/bin/env python3
"""Create a quiesced, verified backup set of the shared platform."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backupset import IMAGES, JENKINS_HOME_EXCLUDES, REQUIRED, archive_directory, digest, expected_versions, validate_tar, verify_backup, write_json  # noqa: E402
from compose import SERVICES, Lab, run  # noqa: E402
from status import containers_ready  # noqa: E402
from transport import validate_https_origin  # noqa: E402

BUILT_SERVICES = ("edge", "jenkins")


class CleanupError(RuntimeError):
    """Summarise the original failure and every cleanup failure without child output."""

    def __init__(self, stage: str, original: BaseException | None, cleanup_errors: list) -> None:
        self.original = original
        self.cleanup_errors = cleanup_errors
        failures = ([(stage, original)] if original is not None else []) + cleanup_errors
        summaries = []
        for label, error in failures:
            summary = f"{label}: {type(error).__name__}"
            if isinstance(error, subprocess.CalledProcessError):
                summary += f" (exit {error.returncode})"
            elif isinstance(error, subprocess.TimeoutExpired):
                summary += " (timed out)"
            summaries.append(summary)
        super().__init__("; ".join(summaries))


def attempt_cleanup(actions, *, stage: str, original: BaseException | None = None) -> None:
    errors = []
    for label, action in actions:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - every cleanup must be attempted
            errors.append((label, error))
    if original is not None or errors:
        raise CleanupError(stage, original, errors) from None


def text(lab: Lab, service: str, *command, **kwargs) -> str:
    return lab.exec(service, *command, **kwargs).decode(errors="replace").strip()


def checkpoint(lab: Lab) -> dict:
    """Health and idleness of the five services; nothing about projects, jobs or agents."""
    for name in ("GITLAB", "JENKINS", "SONAR"):
        validate_https_origin(lab.env[name + "_URL"], lab.env[name + "_HOST"])
    text(lab, "gitlab", "/opt/gitlab/bin/gitlab-healthcheck", "--fail")
    text(lab, "gitlab", "gitlab-rake", "gitlab:check", "SANITIZE=true", timeout=600)
    text(lab, "gitlab", "gitlab-rake", "gitlab:doctor:secrets", timeout=600)
    jenkins_state = json.loads(lab.groovy(
        "import groovy.json.JsonOutput\n"
        "def j = jenkins.model.Jenkins.instance\n"
        "println JsonOutput.toJson([quiet: j.quietingDown, executors: j.numExecutors,\n"
        "  busyExecutors: j.computers.collectMany { it.executors }.count { it.busy },\n"
        "  queueLength: j.queue.items.size()])"))
    # Both calls need the admin credentials: SonarQube forces authentication by default,
    # so an unauthenticated request from inside the container answers HTTP 401.
    sonar = lab.sonar()
    _, activity, _ = sonar.request("GET", "/api/ce/activity?status=PENDING,IN_PROGRESS")
    if activity.get("tasks"):
        raise RuntimeError("SonarQube background tasks are not idle; retry later")
    _, reported, _ = sonar.request("GET", "/api/server/version")
    sonar_version = (reported.decode() if isinstance(reported, (bytes, bytearray)) else str(reported)).strip()
    return {
        "versions": {service: image["tag"] for service, image in lab.versions.items()},
        "gitlab": {"health": "passed", "check": "passed", "doctor": "passed"},
        "jenkins": jenkins_state,
        "sonarqube": {"ce_idle": True, "version": sonar_version},
    }


def running_images(lab: Lab) -> dict[str, dict[str, str]]:
    images = {}
    for service in SERVICES:
        container = lab.dc("ps", "-q", service).decode().strip()
        if not container:
            raise RuntimeError(f"service {service} is not running")
        info = json.loads(run(["docker", "inspect", container]))[0]
        images[service] = {"id": info["Image"]}
        if service not in BUILT_SERVICES:
            images[service]["digest"] = lab.versions[service]["digest"]
    return images


def installed_plugins(lab: Lab) -> dict[str, str]:
    return json.loads(lab.groovy(
        "import groovy.json.JsonOutput\n"
        "println JsonOutput.toJson(jenkins.model.Jenkins.instance.pluginManager.plugins"
        ".collectEntries { [(it.shortName): it.version] })"))


def quiesce_jenkins(lab: Lab) -> None:
    state = lab.groovy(
        "def j = jenkins.model.Jenkins.instance\n"
        "j.doQuietDown()\n"
        "println(j.queue.items.length == 0 && j.computers.every { c -> c.executors.every { !it.busy } } ? 'IDLE' : 'BUSY')"
    ).strip()
    if state != "IDLE":
        raise RuntimeError("Jenkins is busy; retry after running work completes")


def gitlab_safe_to_stop(lab: Lab) -> None:
    probe = (
        "puts((Sidekiq::Workers.new.size == 0 && Sidekiq::Queue.all.sum(&:size) == 0 && "
        "Ci::Build.where(status: ['running', 'pending']).count == 0 && "
        "!%w[artifacts lfs uploads packages external_diffs terraform_state dependency_proxy ci_secure_files]"
        ".any? { |k| Gitlab.config[k]&.object_store&.enabled }) ? 'SAFE' : 'BUSY_OR_OBJECT_STORE')"
    )
    if text(lab, "gitlab", "gitlab-rails", "runner", probe, timeout=300) != "SAFE":
        raise RuntimeError("GitLab has pending work or uses object storage; the backup cannot be made consistent")


def recover_source(lab: Lab, *, gitlab_stopped: bool, sonar_stopped: bool, jenkins_stopped: bool,
                   original: BaseException | None = None) -> None:
    def cancel_quiet() -> None:
        for _ in range(120):
            try:
                lab.groovy('jenkins.model.Jenkins.instance.doCancelQuietDown(); println "READY"')
                return
            except Exception:  # noqa: BLE001 - Jenkins may still be starting
                time.sleep(5)
        raise RuntimeError("Jenkins did not leave quiet mode after the backup")

    def wait_gitlab() -> None:
        """Puma needs a minute after a restart; without this the backup returns while the edge still answers 502."""
        for _ in range(120):
            try:
                lab.exec("gitlab", "/opt/gitlab/bin/gitlab-healthcheck", "--fail", "--max-time", "10")
                return
            except Exception:  # noqa: BLE001 - GitLab is still booting
                time.sleep(5)
        raise RuntimeError("GitLab did not start serving again after the backup")

    def wait_containers() -> None:
        """A restarted SonarQube reports `running/starting` for a while; the backup must not return before it settles."""
        detail = "not checked"
        for _ in range(120):
            ready, detail = containers_ready(lab.repo, lab.root)
            if ready:
                return
            time.sleep(5)
        raise RuntimeError(f"containers were not healthy again after the backup: {detail}")

    actions = []
    if gitlab_stopped:
        actions.append(("start GitLab Sidekiq", lambda: lab.exec("gitlab", "gitlab-ctl", "start", "sidekiq")))
        actions.append(("start GitLab Puma", lambda: lab.exec("gitlab", "gitlab-ctl", "start", "puma")))
        actions.append(("wait for GitLab", wait_gitlab))
    if sonar_stopped:
        actions.append(("start SonarQube", lambda: lab.dc("start", "sonarqube")))
    if jenkins_stopped:
        actions.append(("start Jenkins", lambda: lab.dc("start", "jenkins")))
    if gitlab_stopped or sonar_stopped or jenkins_stopped:
        actions.append(("wait for containers", wait_containers))
    actions.append(("cancel Jenkins quiet mode", cancel_quiet))
    attempt_cleanup(actions, stage="backup operation", original=original)


def create_backup(lab: Lab, *, include_images: bool) -> pathlib.Path:
    started = time.monotonic()
    os.umask(0o077)
    revision = run(["git", "-C", lab.repo, "rev-parse", "HEAD"]).decode().strip()
    if run(["git", "-C", lab.repo, "status", "--porcelain", "--untracked-files=no"]).strip():
        raise RuntimeError("commit tracked changes before taking a backup")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = lab.root / "backups" / stamp
    destination.mkdir(mode=0o700)
    for folder in ("gitlab", "jenkins", "runtime", "sonar", "source"):
        (destination / folder).mkdir(mode=0o700)
    print(f"Backup set: {destination}", flush=True)

    images = running_images(lab)
    plugins = installed_plugins(lab)
    state = checkpoint(lab)
    write_json(destination / "checkpoint.json", state)
    run(["git", "-C", lab.repo, "bundle", "create", destination / "source/code.bundle", "--all"])
    if include_images:
        print("Saving the five running images...", flush=True)
        run(["docker", "image", "save", "-o", destination / IMAGES,
             *sorted({item["id"] for item in images.values()})], timeout=3600)

    gitlab_stopped = sonar_stopped = jenkins_stopped = False
    try:
        quiesce_jenkins(lab)
        jenkins_stopped = True
        lab.dc("stop", "jenkins")
        sonar_stopped = True
        lab.dc("stop", "sonarqube")
        gitlab_safe_to_stop(lab)
        gitlab_stopped = True
        lab.exec("gitlab", "gitlab-ctl", "stop", "puma")
        lab.exec("gitlab", "gitlab-ctl", "stop", "sidekiq")
        before = set(text(lab, "gitlab", "sh", "-c", "ls /var/opt/gitlab/backups/*_gitlab_backup.tar 2>/dev/null || true").splitlines())
        print("Creating the supported GitLab application backup...", flush=True)
        lab.exec("gitlab", "gitlab-backup", "create", timeout=3600)
        after = set(text(lab, "gitlab", "sh", "-c", "ls /var/opt/gitlab/backups/*_gitlab_backup.tar").splitlines())
        new = after - before
        if len(new) != 1:
            raise RuntimeError("gitlab-backup did not produce exactly one new archive")
        archive_path = new.pop()
        backup_id = pathlib.Path(archive_path).name.removesuffix("_gitlab_backup.tar")
        lab.dc("cp", f"gitlab:{archive_path}", str(destination / "gitlab/application.tar"))
        # The archive is a second full copy of GitLab's data inside the container. Once it is
        # in the set it only consumes the volume, and the next backup's before/after listing
        # would have to reason about it.
        lab.exec("gitlab", "rm", "-f", archive_path)
        with (destination / "gitlab/config.tar").open("wb") as handle:
            lab.exec("gitlab", "tar", "-C", "/etc/gitlab", "-cf", "-", ".", output=handle)
        validate_tar(destination / "gitlab/config.tar")
        archive_directory(lab.root / "data/jenkins", destination / "jenkins/home.tar", JENKINS_HOME_EXCLUDES)
        archive_directory(lab.root / "secrets", destination / "runtime/secrets.tar")
        archive_directory(lab.root / "tls", destination / "runtime/tls.tar")
        archive_directory(lab.root / "config", destination / "runtime/config.tar")
        with (destination / "sonar/sonar.dump").open("wb") as handle:
            lab.exec("postgres", "pg_dump", "-U", "sonar", "-d", "sonar", "--format=custom", output=handle)
        lab.exec("postgres", "pg_restore", "--list", input=(destination / "sonar/sonar.dump").read_bytes())
        for path in destination.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        files = list(REQUIRED) + ([IMAGES] if include_images else [])
        manifest = {
            "format": 2,
            "revision": revision,
            "versions": expected_versions(lab.versions),
            "images": images,
            "plugins": plugins,
            "source": lab.env,
            "gitlab_backup_id": backup_id,
            "includes_images": include_images,
            "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started),
            "files": {name: digest(destination / name) for name in files},
        }
        write_json(destination / "manifest.json", manifest)
        verify_backup(destination, lab.versions)
        print(f"Backup verified: {destination}", flush=True)
    finally:
        recover_source(lab, gitlab_stopped=gitlab_stopped, sonar_stopped=sonar_stopped,
                       jenkins_stopped=jenkins_stopped, original=sys.exception())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--include-images", action="store_true", help="also archive the five running images for air-gapped restores")
    arguments = parser.parse_args()
    try:
        create_backup(Lab(arguments.repo, arguments.root), include_images=arguments.include_images)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"backup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
