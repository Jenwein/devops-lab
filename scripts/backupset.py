#!/usr/bin/env python3
"""Backup-set format, verification and archive safety. Pure functions, no Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import posixpath
import re
import tarfile

from bootstrap import atomic_write, valid_domain, valid_port, valid_project

FORMAT = 2
REQUIRED = (
    "checkpoint.json",
    "source/code.bundle",
    "gitlab/application.tar",
    "gitlab/config.tar",
    "jenkins/home.tar",
    "runtime/secrets.tar",
    "runtime/tls.tar",
    "runtime/config.tar",
    "sonar/sonar.dump",
)
IMAGES = "source/images.tar"
JENKINS_HOME_EXCLUDES = ("war", "cache", "caches", "tools", "workspace", "logs", ".cache")


def digest(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_json(path: pathlib.Path, value) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", 0o600)


def expected_versions(versions: dict[str, dict[str, str]]) -> dict[str, str]:
    return {service: image["reference"] for service, image in versions.items()}


def _safe_relative(name: str) -> bool:
    parts = pathlib.PurePosixPath(name).parts
    return bool(parts) and not pathlib.PurePosixPath(name).is_absolute() and ".." not in parts


def verify_backup(directory: pathlib.Path, versions: dict[str, dict[str, str]]) -> dict:
    directory = pathlib.Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("backup missing or symlink")
    for path in [directory, *directory.rglob("*")]:
        if path.is_symlink():
            raise ValueError("backup contains symlink")
        if path.stat().st_mode & 0o077:
            raise ValueError(f"backup permission must exclude group and other: {path.name}")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise ValueError("unsupported backup format")
    if manifest.get("versions") != expected_versions(versions):
        raise ValueError("backup versions do not match this checkout's versions.env")
    if not re.fullmatch("[0-9a-f]{40}", manifest.get("revision", "")):
        raise ValueError("invalid revision")
    files = manifest.get("files", {})
    expected = set(REQUIRED) | ({IMAGES} if manifest.get("includes_images") else set())
    if set(files) != expected:
        missing = expected - set(files)
        if missing:
            raise ValueError("missing backup components: " + ", ".join(sorted(missing)))
        raise ValueError("unexpected backup components: " + ", ".join(sorted(set(files) - expected)))
    for name, checksum in files.items():
        if not _safe_relative(name):
            raise ValueError("unsafe component path")
        path = directory / name
        if not path.is_file() or digest(path) != checksum:
            raise ValueError("missing component or checksum mismatch: " + name)
    return manifest


def identity_from_manifest(manifest: dict) -> dict[str, str]:
    source = manifest["source"]
    return {
        "project": source["COMPOSE_PROJECT_NAME"],
        "base_domain": source["BASE_DOMAIN"],
        "https_port": source["HTTPS_PORT"],
        "ssh_port": source["GITLAB_SSH_PORT"],
        "bind_address": source.get("BIND_ADDRESS", "0.0.0.0"),
    }


def validate_destination(manifest: dict, root: pathlib.Path, project: str, domain: str,
                         https_port: int, ssh_port: int, *, rehearsal: bool) -> None:
    try:
        valid_project(project)
        domain = valid_domain(domain)
        valid_port(str(https_port))
        valid_port(str(ssh_port))
    except argparse.ArgumentTypeError as error:
        raise ValueError(str(error)) from error
    source = manifest["source"]
    source_root = pathlib.Path(source["DEVOPS_LAB_ROOT"]).resolve()
    root = pathlib.Path(root)
    if root.is_symlink():
        raise ValueError("destination symlink forbidden")
    root = root.resolve()
    if root == pathlib.Path("/") or root == source_root or source_root in root.parents or root in source_root.parents:
        raise ValueError("destination overlaps source root")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("destination must be empty")
    if int(https_port) == int(ssh_port):
        raise ValueError("HTTPS and SSH ports must be different")
    if rehearsal:
        same = [
            label for label, ours, theirs in (
                ("project", project, source["COMPOSE_PROJECT_NAME"]),
                ("domain", domain, source["BASE_DOMAIN"]),
                ("HTTPS port", str(https_port), source["HTTPS_PORT"]),
                ("SSH port", str(ssh_port), source["GITLAB_SSH_PORT"]),
            ) if ours == theirs
        ]
        if same:
            raise ValueError("rehearsal identity must differ from the source in: " + ", ".join(same))


def _link_stays_inside(member: tarfile.TarInfo, name: str) -> bool:
    """A link may only point at something the archive itself carries.

    A symlink target is relative to the member's own directory; a hard link target is
    relative to the archive root. Either way an absolute or escaping target is refused.
    """
    target = member.linkname.removeprefix("./")
    if pathlib.PurePosixPath(target).is_absolute():
        return False
    if member.issym():
        target = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    else:
        target = posixpath.normpath(target)
    return _safe_relative(target)


def validate_tar(path: pathlib.Path) -> None:
    with tarfile.open(path) as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if not _safe_relative(name) and member.name not in (".", "./"):
                raise ValueError("unsafe archive member")
            if member.isfile() or member.isdir():
                continue
            # GitLab keeps hashed symlinks beside the certificates in /etc/gitlab/trusted-certs.
            if (member.issym() or member.islnk()) and _link_stays_inside(member, name):
                continue
            raise ValueError("unsafe archive member")


def archive_directory(source: pathlib.Path, target: pathlib.Path, excludes: tuple[str, ...] = ()) -> None:
    """Tar a directory owned by this user, skipping excluded paths, symlinks and special files."""
    source = pathlib.Path(source)

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        relative = info.name.removeprefix("./")
        if any(relative == item or relative.startswith(item + "/") for item in excludes):
            return None
        if not (info.isfile() or info.isdir()):
            return None
        return info

    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle, tarfile.open(fileobj=handle, mode="w") as archive:
        archive.add(source, arcname=".", filter=keep)
    os.chmod(target, 0o600)
    validate_tar(target)


def extract_tar(path: pathlib.Path, target: pathlib.Path, *, exclude: tuple[str, ...] = ()) -> None:
    validate_tar(path)
    target = pathlib.Path(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    with tarfile.open(path) as archive:
        members = [
            member for member in archive
            if member.name.removeprefix("./") not in exclude
            and not any(member.name.removeprefix("./").startswith(item + "/") for item in exclude)
        ]
        archive.extractall(target, members=members, filter="data")
