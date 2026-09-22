#!/usr/bin/env python3
"""Report container health and TLS-verified endpoint status."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from compose import SERVICES, compose_command, compose_environment, read_env  # noqa: E402

CHECKS = (
    ("GITLAB_HOST", "/users/sign_in", ("200",)),
    ("JENKINS_HOST", "/login", ("200", "302")),
    ("SONAR_HOST", "/api/system/status", ("200",)),
)


def compose_ps(repo: pathlib.Path, root: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*compose_command(repo, root), "ps", "--all", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=compose_environment(repo, root),
    )


def parse_compose_rows(payload: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    rows: list[dict[str, object]] = []
    position = 0
    while position < len(payload):
        while position < len(payload) and payload[position].isspace():
            position += 1
        if position >= len(payload):
            break
        value, position = decoder.raw_decode(payload, position)
        rows.extend(value if isinstance(value, list) else [value])
    return rows


def containers_ready(repo: pathlib.Path, root: pathlib.Path) -> tuple[bool, str]:
    result = compose_ps(repo, root)
    if result.returncode:
        return False, result.stderr.strip()
    try:
        rows = parse_compose_rows(result.stdout)
    except json.JSONDecodeError as error:
        return False, f"invalid compose status: {error}"
    expected = set(SERVICES)
    present = {row.get("Service") for row in rows}
    unhealthy = [
        f"{row.get('Service')}={row.get('State')}/{row.get('Health') or 'none'}"
        for row in rows
        if row.get("Service") in expected
        if row.get("State") != "running" or row.get("Health") != "healthy"
    ]
    missing = sorted(expected - present)
    details = unhealthy + ([f"missing={','.join(missing)}"] if missing else [])
    return not details, "; ".join(details) if details else "all containers healthy"


def endpoint(
    root: pathlib.Path, host: str, port: str, path: str, address: str = "127.0.0.1", expected=("200",)
) -> tuple[bool, str]:
    result = subprocess.run(
        [
            "curl", "--silent", "--show-error", "--output", "/dev/null",
            "--write-out", "%{http_code}",
            "--cacert", str(root / "tls" / "ca.crt"),
            "--noproxy", "*",
            "--resolve", f"{host}:{port}:{address}",
            "--max-time", "15",
            f"https://{host}:{port}{path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    code = result.stdout.strip()
    return result.returncode == 0 and code in expected, code or result.stderr.strip()


def resolve_address(values: dict[str, str]) -> str:
    bind = values.get("BIND_ADDRESS", "0.0.0.0")
    return "127.0.0.1" if bind in ("0.0.0.0", "::") else bind


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    values = read_env(root / "config" / "runtime.env")
    address = resolve_address(values)
    deadline = time.monotonic() + arguments.wait
    while True:
        healthy, details = containers_ready(arguments.repo, root)
        results = [
            (values[key], *endpoint(root, values[key], values["HTTPS_PORT"], path, address, expected))
            for key, path, expected in CHECKS
        ]
        all_ready = healthy and all(ok for _, ok, _ in results)
        if all_ready or time.monotonic() >= deadline:
            break
        time.sleep(5)
    print(f"containers: {details}")
    for host, _, detail in results:
        print(f"{host}: {detail}")
    return 0 if all_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
