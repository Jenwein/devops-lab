#!/usr/bin/env python3
"""Compose command construction, runtime environment access and the Lab wrapper."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bootstrap import atomic_write  # noqa: E402

SERVICES = ("edge", "gitlab", "jenkins", "postgres", "sonarqube")
IMAGE_KEYS = {service: f"{service.upper()}_IMAGE" for service in SERVICES}
IMAGE_PATTERN = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9._/-]*):(?P<tag>[A-Za-z0-9._-]+)@sha256:(?P<digest>[0-9a-f]{64})$"
)
PINS_FILE = "images-pinned.json"


class CommandError(RuntimeError):
    """A child command failed. Its output is never included in the message."""


def read_env(path: pathlib.Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; blank lines and # comments are ignored."""
    values: dict[str, str] = {}
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"invalid line in {pathlib.Path(path).name}: {line!r}")
        values[key.strip()] = value.strip()
    return values


def parse_image_reference(reference: str) -> dict[str, str]:
    match = IMAGE_PATTERN.match(reference)
    if not match:
        raise ValueError(f"image reference must be name:tag@sha256:digest, got {reference!r}")
    return {"reference": reference, **match.groupdict()}


def load_versions(repo: pathlib.Path) -> dict[str, dict[str, str]]:
    values = read_env(pathlib.Path(repo) / "versions.env")
    expected = set(IMAGE_KEYS.values())
    missing = sorted(expected - set(values))
    if missing:
        raise ValueError(f"versions.env is missing {', '.join(missing)}")
    unknown = sorted(set(values) - expected)
    if unknown:
        raise ValueError(f"versions.env has unknown entries {', '.join(unknown)}")
    return {service: parse_image_reference(values[key]) for service, key in IMAGE_KEYS.items()}


def service_image(repo: pathlib.Path, root: pathlib.Path, service: str) -> str:
    """The image a service actually runs: a pinned id when the root has one, else versions.env.

    `scripts/lab` runs the Jenkins image directly, outside Compose, to build the Java trust
    store. After an air-gapped restore the versions.env reference names a registry the host
    cannot reach and no local image carries that name, so the helper has to read the same
    pins Compose is given.
    """
    pins = pathlib.Path(root) / "config" / PINS_FILE
    if pins.exists():
        pinned = json.loads(pins.read_text()).get("services", {}).get(service, {}).get("image")
        if pinned:
            return pinned
    return load_versions(repo)[service]["reference"]


def compose_command(repo: pathlib.Path, root: pathlib.Path, overlay: pathlib.Path | None = None) -> list[str]:
    """The only place the docker compose invocation is assembled.

    `<root>/config/images-pinned.json` is the one automatic overlay: a restore from an
    image-carrying set writes it so that every later invocation, including `scripts/lab up`,
    keeps using the loaded images instead of the registry references in versions.env.
    """
    repo = pathlib.Path(repo)
    root = pathlib.Path(root)
    command = [
        "docker", "compose",
        "--env-file", str(repo / "versions.env"),
        "--env-file", str(root / "config" / "runtime.env"),
        "-f", str(repo / "compose.yaml"),
    ]
    pins = root / "config" / PINS_FILE
    if pins.exists():
        command += ["-f", str(pins)]
    if overlay is not None:
        command += ["-f", str(overlay)]
    return command


def compose_environment(repo: pathlib.Path, root: pathlib.Path, base: dict | None = None) -> dict[str, str]:
    """The environment a compose invocation must run with.

    Compose gives the ambient environment precedence over `--env-file`, so a shell that
    exported DEVOPS_LAB_ROOT for one platform would silently redirect the bind mounts of
    a stack belonging to another root - a restore into a new root would then mount, and
    overwrite, the running source's data. Re-applying both files on top keeps the files
    authoritative.
    """
    environment = dict(os.environ if base is None else base)
    environment.update(read_env(pathlib.Path(repo) / "versions.env"))
    environment.update(read_env(pathlib.Path(root) / "config" / "runtime.env"))
    return environment


def run(command, *, input=None, output=None, timeout=1200, failure_log=None, redactions=(), env=None) -> bytes:
    """Run a child process. On failure write redacted stderr privately and raise CommandError."""
    completed = subprocess.run(
        [str(part) for part in command],
        input=input,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    if completed.returncode:
        name = pathlib.Path(str(command[0])).name
        detail = ""
        if failure_log is not None:
            text = completed.stderr
            for secret in redactions:
                if len(secret) >= 8:
                    text = text.replace(secret, b"[REDACTED]")
            atomic_write(pathlib.Path(failure_log), text[-65536:], 0o600)
            detail = f"; diagnostics in {failure_log}"
        raise CommandError(f"{name} failed (exit {completed.returncode}){detail}")
    return completed.stdout or b""


class Lab:
    """A deployed runtime root plus the repository that drives it."""

    def __init__(self, repo: pathlib.Path, root: pathlib.Path) -> None:
        self.repo = pathlib.Path(repo).resolve()
        self.root = pathlib.Path(root).resolve()
        self.env = read_env(self.root / "config" / "runtime.env")
        self.versions = load_versions(self.repo)
        self.overlay: pathlib.Path | None = None

    @property
    def project(self) -> str:
        return self.env["COMPOSE_PROJECT_NAME"]

    @property
    def ca_file(self) -> str:
        return str(self.root / "tls" / "ca.crt")

    @property
    def resolve_address(self) -> str:
        bind = self.env.get("BIND_ADDRESS", "0.0.0.0")
        return "127.0.0.1" if bind in ("0.0.0.0", "::") else bind

    def secret(self, name: str) -> str:
        return (self.root / "secrets" / name).read_text().strip()

    def redactions(self) -> list[bytes]:
        values = []
        for path in (self.root / "secrets").rglob("*"):
            if path.is_file():
                values.append(path.read_bytes().strip())
        return values

    def run(self, command, **kwargs) -> bytes:
        return run(
            command,
            failure_log=self.root / "evidence" / "last-failure.log",
            redactions=self.redactions(),
            **kwargs,
        )

    def dc(self, *arguments, **kwargs) -> bytes:
        kwargs.setdefault("env", compose_environment(self.repo, self.root))
        return self.run([*compose_command(self.repo, self.root, self.overlay), *arguments], **kwargs)

    def exec(self, service: str, *arguments, **kwargs) -> bytes:
        return self.dc("exec", "-T", service, *arguments, **kwargs)

    def transport(self, url: str, headers: dict[str, str] | None = None):
        from transport import UrlTransport
        return UrlTransport(url, self.ca_file, headers=headers, resolve=self.resolve_address)

    def jenkins(self):
        from transport import basic_auth_headers
        client = self.transport(
            self.env["JENKINS_URL"],
            basic_auth_headers("admin", self.secret("jenkins/jenkins_admin_password")),
        )
        _, crumb, headers = client.request("GET", "/crumbIssuer/api/json")
        client.headers[crumb["crumbRequestField"]] = crumb["crumb"]
        cookie = next((value for key, value in headers.items() if key.lower() == "set-cookie"), None)
        if cookie:
            client.headers["Cookie"] = cookie.split(";", 1)[0]
        return client

    def groovy(self, script: str) -> str:
        _, data, _ = self.jenkins().request("POST", "/scriptText", {"script": script})
        return data.decode() if isinstance(data, bytes) else json.dumps(data)

    def gitlab(self):
        return self.transport(
            self.env["GITLAB_URL"] + "/api/v4",
            {"PRIVATE-TOKEN": self.secret("gitlab_root_token")},
        )

    def sonar(self):
        from transport import basic_auth_headers
        return self.transport(
            self.env["SONAR_URL"],
            basic_auth_headers("admin", self.secret("sonar_admin_password")),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    subcommands.add_parser("command")
    image = subcommands.add_parser("image")
    image.add_argument("service", choices=SERVICES)
    invoke = subcommands.add_parser("run", help="exec docker compose with the env files authoritative")
    invoke.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.subcommand == "run":
        # Rendering the argv and running it from a shell would leave the ambient
        # environment in charge of every interpolated key, so exec it from here.
        rest = arguments.arguments[1:] if arguments.arguments[:1] == ["--"] else arguments.arguments
        command = [*compose_command(arguments.repo, arguments.root), *rest]
        os.execvpe(command[0], command, compose_environment(arguments.repo, arguments.root))
    if arguments.subcommand == "command":
        print("\n".join(compose_command(arguments.repo, arguments.root)))
    else:
        print(service_image(arguments.repo, arguments.root, arguments.service))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
