#!/usr/bin/env python3
"""Create or update the private runtime root: directories, secrets, TLS and runtime.env."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile

DEFAULT_ROOT = pathlib.Path.home() / "devops-lab"
DIRECTORIES = (
    "secrets", "secrets/jenkins", "tls", "tls/trust", "config",
    "config/jenkins/casc.d", "config/sonarqube/plugins", "data", "evidence", "backups",
)
DATA_DIRECTORIES = (
    "gitlab/config", "gitlab/config/trusted-certs", "gitlab/logs", "gitlab/data",
    "jenkins", "postgres", "sonarqube/data", "sonarqube/logs", "sonarqube/temp",
)
SECRET_NAMES = (
    "gitlab_root_password",
    "sonar_db_password",
    "sonar_admin_password",
    "jenkins/jenkins_admin_password",
    "jenkins/gitlab_webhook_secret",
)
MEMORY_DEFAULTS = {
    "EDGE_MEMORY_LIMIT": "256m",
    "GITLAB_MEMORY_LIMIT": "8g",
    "JENKINS_MEMORY_LIMIT": "3g",
    "POSTGRES_MEMORY_LIMIT": "2g",
    "SONARQUBE_MEMORY_LIMIT": "5g",
}
SITE_KEYS = {
    "project": "COMPOSE_PROJECT_NAME",
    "base_domain": "BASE_DOMAIN",
    "bind_address": "BIND_ADDRESS",
    "https_port": "HTTPS_PORT",
    "ssh_port": "GITLAB_SSH_PORT",
}
SITE_DEFAULTS = {
    "project": "devops-lab",
    "base_domain": "devops.test",
    "bind_address": "0.0.0.0",
    "https_port": "443",
    "ssh_port": "2224",
}
ENV_FALLBACKS = {
    "project": "DEVOPS_LAB_PROJECT",
    "base_domain": "DEVOPS_LAB_DOMAIN",
    "bind_address": "DEVOPS_LAB_BIND",
    "https_port": "DEVOPS_LAB_HTTPS_PORT",
    "ssh_port": "DEVOPS_LAB_SSH_PORT",
}


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def valid_domain(value: str) -> str:
    domain = value.lower().rstrip(".")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(domain) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", domain):
        raise argparse.ArgumentTypeError("base domain is invalid")
    return domain


def valid_project(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value):
        raise argparse.ArgumentTypeError("project must use lowercase letters, numbers, _ or -")
    return value


def valid_bind_address(value: str) -> str:
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value) or any(int(part) > 255 for part in value.split(".")):
        raise argparse.ArgumentTypeError("bind address must be an IPv4 address such as 0.0.0.0")
    return value


VALIDATORS = {
    "project": valid_project,
    "base_domain": valid_domain,
    "bind_address": valid_bind_address,
    "https_port": valid_port,
    "ssh_port": valid_port,
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=pathlib.Path,
                        default=pathlib.Path(os.environ.get("DEVOPS_LAB_ROOT", DEFAULT_ROOT)))
    result.add_argument("--project", type=valid_project)
    result.add_argument("--base-domain", type=valid_domain)
    result.add_argument("--bind-address", type=valid_bind_address)
    result.add_argument("--https-port", type=valid_port)
    result.add_argument("--ssh-port", type=valid_port)
    return result


def atomic_write(path: pathlib.Path, content: str | bytes, mode: int) -> None:
    data = content.encode() if isinstance(content, str) else content
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def run_openssl(arguments: list[str]) -> None:
    result = subprocess.run(
        ["openssl", *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"OpenSSL failed: {result.stderr.strip()}")


def ensure_ca(tls: pathlib.Path) -> None:
    key = tls / "ca.key"
    certificate = tls / "ca.crt"
    if key.exists() != certificate.exists():
        raise RuntimeError("CA key/certificate pair is incomplete; refusing to replace it")
    if not key.exists():
        run_openssl(["genrsa", "-out", str(key), "4096"])
        os.chmod(key, 0o600)
        run_openssl(
            [
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-key",
                str(key),
                "-days",
                "3650",
                "-subj",
                "/CN=DevOps Lab Local CA/O=DevOps Lab",
                "-out",
                str(certificate),
            ]
        )
    os.chmod(key, 0o600)
    os.chmod(certificate, 0o644)


def ensure_server_certificate(tls: pathlib.Path, hosts: list[str]) -> None:
    key = tls / "server.key"
    certificate = tls / "server.crt"
    if key.exists() != certificate.exists():
        raise RuntimeError("server key/certificate pair is incomplete; refusing to replace it")
    if certificate.exists() and all(
        subprocess.run(
            [
                "openssl",
                "verify",
                "-CAfile",
                str(tls / "ca.crt"),
                "-verify_hostname",
                host,
                str(certificate),
            ],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
        for host in hosts
    ):
        os.chmod(key, 0o600)
        os.chmod(certificate, 0o644)
        return

    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = pathlib.Path(temporary)
        new_key = temporary_path / "server.key"
        new_certificate = temporary_path / "server.crt"
        request = temporary_path / "server.csr"
        extensions = temporary_path / "server.ext"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={','.join(f'DNS:{host}' for host in hosts)}\n"
        )
        run_openssl(["genrsa", "-out", str(new_key), "3072"])
        run_openssl(
            ["req", "-new", "-sha256", "-key", str(new_key), "-subj", "/CN=" + hosts[0], "-out", str(request)]
        )
        run_openssl(
            [
                "x509",
                "-req",
                "-sha256",
                "-in",
                str(request),
                "-CA",
                str(tls / "ca.crt"),
                "-CAkey",
                str(tls / "ca.key"),
                "-CAcreateserial",
                "-days",
                "825",
                "-extfile",
                str(extensions),
                "-out",
                str(new_certificate),
            ]
        )
        atomic_write(key, new_key.read_bytes(), 0o600)
        atomic_write(certificate, new_certificate.read_bytes(), 0o644)
    os.chmod(key, 0o600)
    os.chmod(certificate, 0o644)


def ensure_secret(path: pathlib.Path) -> None:
    """Create a random secret once; never rotate an existing one. No trailing newline."""
    if not path.exists():
        random_value = secrets.token_urlsafe(32)
        # SonarQube requires every character class; URL-safe text alone can omit one.
        prefix = "Aa1!" if path.name.endswith("_password") else ""
        atomic_write(path, prefix + random_value, 0o600)
    os.chmod(path, 0o600)


def read_existing_env(path: pathlib.Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def resolve_site(arguments: argparse.Namespace, existing: dict[str, str] | None) -> dict[str, str]:
    """Explicit flags win; then the existing file; then environment fallbacks; then defaults."""
    site: dict[str, str] = {}
    for flag, key in SITE_KEYS.items():
        passed = getattr(arguments, flag)
        if passed is not None:
            site[flag] = str(passed)
        elif existing is not None and key in existing:
            site[flag] = existing[key]
        elif os.environ.get(ENV_FALLBACKS[flag]):
            site[flag] = str(VALIDATORS[flag](os.environ[ENV_FALLBACKS[flag]]))
        else:
            site[flag] = SITE_DEFAULTS[flag]
    if site["https_port"] == site["ssh_port"]:
        raise RuntimeError("HTTPS and SSH ports must be different")
    return site


def render_runtime_env(root: pathlib.Path, site: dict[str, str], existing: dict[str, str] | None) -> str:
    hosts = [f"{name}.{site['base_domain']}" for name in ("gitlab", "jenkins", "sonar")]
    suffix = "" if site["https_port"] == "443" else f":{site['https_port']}"
    kept = existing or {}
    values = {
        "COMPOSE_PROJECT_NAME": site["project"],
        "DEVOPS_LAB_ROOT": str(root),
        "BASE_DOMAIN": site["base_domain"],
        "GITLAB_HOST": hosts[0],
        "JENKINS_HOST": hosts[1],
        "SONAR_HOST": hosts[2],
        "GITLAB_URL": f"https://{hosts[0]}{suffix}",
        "JENKINS_URL": f"https://{hosts[1]}{suffix}",
        "SONAR_URL": f"https://{hosts[2]}{suffix}",
        "BIND_ADDRESS": site["bind_address"],
        "HTTPS_PORT": site["https_port"],
        "GITLAB_SSH_PORT": site["ssh_port"],
        "DEVOPS_LAB_UID": kept.get("DEVOPS_LAB_UID", str(os.getuid())),
        "DEVOPS_LAB_GID": kept.get("DEVOPS_LAB_GID", str(os.getgid())),
    }
    for key, default in MEMORY_DEFAULTS.items():
        values[key] = kept.get(key, default)
    for key, value in kept.items():
        if key not in values:
            values[key] = value
    return "".join(f"{key}={value}\n" for key, value in values.items())


def setup(arguments: argparse.Namespace) -> None:
    root = arguments.root.expanduser().resolve()
    if root == pathlib.Path("/"):
        raise RuntimeError("runtime root cannot be /")
    marker = root / ".devops-lab-project"
    runtime_env = root / "config" / "runtime.env"
    existing = read_existing_env(runtime_env)
    site = resolve_site(arguments, existing)

    if root.exists() and not root.is_dir():
        raise RuntimeError("runtime root exists and is not a directory")
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise RuntimeError("runtime root is non-empty and has no DevOps Lab ownership marker")
    if marker.exists() and marker.read_text().strip() != site["project"]:
        raise RuntimeError("runtime root belongs to a different project")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for name in DIRECTORIES:
        path = root / name
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    for relative in DATA_DIRECTORIES:
        (root / "data" / relative).mkdir(parents=True, mode=0o700, exist_ok=True)

    atomic_write(marker, site["project"] + "\n", 0o600)
    for name in SECRET_NAMES:
        ensure_secret(root / "secrets" / name)
    hosts = [f"{name}.{site['base_domain']}" for name in ("gitlab", "jenkins", "sonar")]
    ensure_ca(root / "tls")
    ensure_server_certificate(root / "tls", hosts)
    atomic_write(runtime_env, render_runtime_env(root, site, existing), 0o600)
    print(f"Runtime initialized at {root}")


def main() -> int:
    try:
        setup(parser().parse_args())
    except (OSError, RuntimeError) as error:
        print(f"setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
