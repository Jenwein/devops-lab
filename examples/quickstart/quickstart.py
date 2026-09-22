#!/usr/bin/env python3
"""Prove the platform end to end: team, agent, push, build, analysis, quality gate."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))
from backupset import write_json  # noqa: E402
from bootstrap import atomic_write  # noqa: E402
from compose import Lab, compose_environment, run  # noqa: E402
from teams import add_team  # noqa: E402

TEAM = "demo"
NODE = "demo-linux"
PROJECT = "hello"
SONAR_KEY = "demo-hello"
AGENT_COMPOSE = REPOSITORY / "examples" / "agents" / "linux" / "compose.yaml"
SAMPLE = REPOSITORY / "examples" / "quickstart" / "hello"
# Fixed dates keep the sample commit deterministic for a given parent and tree.
SAMPLE_DATE = "2026-01-01T00:00:00+00:00"


def agent_compose(lab: Lab) -> list[str]:
    """The agent gets a Compose project of its own.

    The file names itself `${PLATFORM_PROJECT}-quickstart` rather than from
    COMPOSE_PROJECT_NAME, because -p rewrites COMPOSE_PROJECT_NAME for interpolation and
    the external platform network would then no longer resolve. -p is still needed: the
    COMPOSE_PROJECT_NAME that runtime.env supplies outranks a `name:` key, so without it
    the agent lands in the core project and `scripts/lab down` takes it with the platform.
    """
    return ["docker", "compose", "-p", f"{lab.project}-quickstart",
            "--env-file", str(lab.root / "config" / "runtime.env"), "-f", str(AGENT_COMPOSE)]


def ensure_node(lab: Lab) -> None:
    secret = lab.groovy(f"""
import hudson.model.Node
import hudson.slaves.DumbSlave
import hudson.slaves.JNLPLauncher
import jenkins.model.Jenkins
def jenkins = Jenkins.get()
if (jenkins.getNode('{NODE}') == null) {{
  def launcher = new JNLPLauncher(true)
  launcher.setWebSocket(true)
  def node = new DumbSlave('{NODE}', '/home/agent/work', launcher)
  node.setLabelString('{NODE}')
  node.setNumExecutors(1)
  node.setMode(Node.Mode.EXCLUSIVE)
  jenkins.addNode(node)
}}
println jenkins.getComputer('{NODE}').getJnlpMac()
""").strip().splitlines()[-1]
    if len(secret) < 32:
        raise RuntimeError("Jenkins did not return an inbound agent secret")
    atomic_write(lab.root / "secrets" / "quickstart_agent_secret", secret, 0o600)


def agent_environment(lab: Lab) -> dict[str, str]:
    """PLATFORM_PROJECT names the platform this agent joins, which -p would otherwise mask."""
    return {**compose_environment(lab.repo, lab.root), "PLATFORM_PROJECT": lab.project}


def start_agent(lab: Lab) -> None:
    lab.run([*agent_compose(lab), "up", "--detach", "--build"],
            timeout=1200, env=agent_environment(lab))


def stop_agent(lab: Lab) -> None:
    """Remove the agent stack. Its Compose file is under examples/, out of reach of a
    bare `docker compose -p <project>-quickstart down` run from the repository root."""
    lab.run([*agent_compose(lab), "down"], timeout=300, env=agent_environment(lab))


def wait_node_online(lab: Lab, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lab.groovy(f"println jenkins.model.Jenkins.get().getComputer('{NODE}')?.isOnline()").strip().endswith("true"):
            return
        time.sleep(5)
    raise RuntimeError(f"agent {NODE} did not connect")


def ensure_sonar_project(lab: Lab) -> None:
    sonar = lab.sonar()
    _, found, _ = sonar.request("GET", "/api/projects/search?projects=" + SONAR_KEY)
    if any(component.get("key") == SONAR_KEY for component in found.get("components", [])):
        return
    sonar.request("POST", "/api/projects/create",
                  {"project": SONAR_KEY, "name": "Demo Hello", "visibility": "private", "mainBranch": "main"})


def ensure_gitlab_project(lab: Lab) -> dict:
    """Create the sample project unless it is already there.

    A 301 means the path only holds a redirect left behind by a rename or a scheduled
    deletion, so the project has to be created rather than reused.
    """
    gitlab = lab.gitlab()
    encoded = urllib.parse.quote(f"{TEAM}/{PROJECT}", safe="")
    status, payload, _ = gitlab.request("GET", f"/projects/{encoded}", expected=(200, 301, 404))
    if status == 200:
        return payload
    _, groups, _ = gitlab.request("GET", "/groups?search=" + TEAM)
    group = next(group for group in groups if group.get("full_path") == TEAM)
    _, created, _ = gitlab.request("POST", "/projects", {
        "name": PROJECT, "path": PROJECT, "namespace_id": group["id"], "visibility": "private",
        "initialize_with_readme": "false", "default_branch": "main"}, expected=(201,))
    return created


def prepare_sample(work: pathlib.Path) -> None:
    """Copy the sample without local build output, so the pushed tree only holds sources."""
    shutil.copytree(SAMPLE, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".scannerwork"))


# Runs inside the one-off container. `main` is a protected branch, so the new commit is
# built on whatever the remote already has and the push is always a fast-forward; when
# the sample matches the remote tree nothing is committed and nothing is pushed.
PUSH_SCRIPT = """set -eu
cd "${WORK}/src"
git init -q -b main
git remote add origin "${REMOTE}"
if git fetch -q --depth=1 origin main 2>/dev/null; then git reset -q --soft FETCH_HEAD; fi
git add -A
if git diff --quiet --cached; then
  git rev-parse HEAD > "${WORK}/sha"
else
  git commit -q -m 'quickstart sample'
  git rev-parse HEAD > "${WORK}/sha"
  git push -q origin HEAD:refs/heads/main
fi
"""


def push_sample(lab: Lab, project: dict) -> str:
    """Push the sample from a one-off container on the platform network, so the host needs no DNS."""
    with tempfile.TemporaryDirectory() as temporary:
        prepare_sample(pathlib.Path(temporary) / "src")
        askpass = pathlib.Path(temporary) / "askpass.sh"
        askpass.write_text("#!/bin/sh\ncase \"$1\" in *sername*) echo oauth2 ;; *) echo \"$GITLAB_TOKEN\" ;; esac\n")
        askpass.chmod(0o700)
        # The token travels in a 0600 env file, never on a command line.
        env_file = pathlib.Path(temporary) / "env.list"
        atomic_write(env_file, f"GITLAB_TOKEN={lab.secret('gitlab_root_token')}\n", 0o600)
        run(["docker", "run", "--rm", "--network", f"{lab.project}_edge",
             "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "sh",
             "-v", f"{temporary}:/work", "-v", f"{lab.ca_file}:/ca.crt:ro",
             "--env-file", str(env_file),
             "-e", "WORK=/work", "-e", "GIT_ASKPASS=/work/askpass.sh", "-e", "GIT_TERMINAL_PROMPT=0",
             "-e", "GIT_SSL_CAINFO=/ca.crt", "-e", "HOME=/work",
             "-e", f"REMOTE={project['http_url_to_repo']}",
             "-e", "GIT_AUTHOR_NAME=quickstart", "-e", "GIT_AUTHOR_EMAIL=quickstart@devops.invalid",
             "-e", "GIT_COMMITTER_NAME=quickstart", "-e", "GIT_COMMITTER_EMAIL=quickstart@devops.invalid",
             "-e", f"GIT_AUTHOR_DATE={SAMPLE_DATE}", "-e", f"GIT_COMMITTER_DATE={SAMPLE_DATE}",
             lab.versions["jenkins"]["reference"], "-eu", "-c", PUSH_SCRIPT], timeout=300)
        return (pathlib.Path(temporary) / "sha").read_text().strip()


def trigger_scan(lab: Lab) -> None:
    lab.jenkins().request("POST", f"/job/{TEAM}/job/gitlab/build", expected=(200, 201, 302))


def discovered_job(lab: Lab) -> str | None:
    """branch-api names a discovered project after its GitLab path, url-encoded into a legal item name.

    A project in group `demo` therefore becomes the item `demo%2Fhello`, not `hello`,
    and reaching it over HTTP needs that name encoded a second time.
    """
    status, folder, _ = lab.jenkins().request("GET", f"/job/{TEAM}/job/gitlab/api/json", expected=(200, 404))
    if status != 200:
        return None
    wanted = {PROJECT, f"{TEAM}/{PROJECT}"}
    for job in folder.get("jobs", []):
        name = job.get("name", "")
        if urllib.parse.unquote(name) in wanted:
            return f"/job/{TEAM}/job/gitlab/job/{urllib.parse.quote(name, safe='')}"
    return None


BUILD_TREE = "builds[number,result,url,actions[lastBuiltRevision[SHA1]]]"


def built_revision(build: dict) -> str | None:
    for action in build.get("actions", []):
        revision = (action or {}).get("lastBuiltRevision") or {}
        if revision.get("SHA1"):
            return revision["SHA1"]
    return None


def wait_build(lab: Lab, sha: str, timeout: int = 900) -> dict:
    """Wait for the build of the pushed revision.

    Judging `lastBuild` instead would report whatever the branch built before this
    push - an older failure makes the run abort before the new build even starts.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = discovered_job(lab)
        if job:
            status, payload, _ = lab.jenkins().request(
                "GET", f"{job}/job/main/api/json?tree={BUILD_TREE}", expected=(200, 404))
            for build in payload.get("builds", []) if status == 200 else []:
                if built_revision(build) == sha and build.get("result"):
                    if build["result"] != "SUCCESS":
                        raise RuntimeError(f"quickstart build finished with {build['result']}: {build.get('url')}")
                    return build
        time.sleep(10)
    raise RuntimeError(f"no finished build for {sha} in time")


def commit_status(lab: Lab, project_id: int, sha: str) -> str:
    _, statuses, _ = lab.gitlab().request("GET", f"/projects/{project_id}/repository/commits/{sha}/statuses")
    return next((item["status"] for item in statuses if item.get("status") == "success"), "missing")


def quality_gate(lab: Lab) -> str:
    _, payload, _ = lab.sonar().request("GET", "/api/qualitygates/project_status?projectKey=" + SONAR_KEY)
    return payload["projectStatus"]["status"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=REPOSITORY)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--stop", action="store_true",
                        help="stop and remove the quickstart agent, then exit")
    arguments = parser.parse_args(argv)
    lab = Lab(arguments.repo, arguments.root)
    if arguments.stop:
        stop_agent(lab)
        return 0
    started = time.monotonic()
    print("Team:", add_team(lab, TEAM)["gitlab_group"], flush=True)
    ensure_node(lab)
    start_agent(lab)
    wait_node_online(lab)
    print("Agent online", flush=True)
    ensure_sonar_project(lab)
    project = ensure_gitlab_project(lab)
    sha = push_sample(lab, project)
    print("Pushed", sha, flush=True)
    trigger_scan(lab)
    build = wait_build(lab, sha)
    print("Build", build["number"], build["result"], flush=True)
    status = commit_status(lab, int(project["id"]), sha)
    gate = quality_gate(lab)
    evidence = {"commit": sha, "build_number": build["number"], "build_result": build["result"],
                "jenkins_job": discovered_job(lab),
                "gitlab_commit_status": status, "sonar_quality_gate": gate,
                "elapsed_seconds": round(time.monotonic() - started)}
    write_json(lab.root / "evidence" / "quickstart.json", evidence)
    print(json.dumps(evidence, indent=2))
    if status != "success" or gate != "OK":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
