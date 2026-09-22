import hashlib
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import backupset  # noqa: E402
import compose  # noqa: E402

VERSIONS = compose.load_versions(REPOSITORY)


class BackupSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.backup = self.base / "backup"
        self.backup.mkdir(mode=0o700)
        self.manifest = {
            "format": 2, "revision": "a" * 40, "versions": backupset.expected_versions(VERSIONS),
            "includes_images": False,
            "source": {"DEVOPS_LAB_ROOT": str(self.base / "source"), "COMPOSE_PROJECT_NAME": "source",
                       "BASE_DOMAIN": "source.test", "HTTPS_PORT": "443", "GITLAB_SSH_PORT": "2224",
                       "BIND_ADDRESS": "0.0.0.0"},
            "files": {},
        }
        for name in backupset.REQUIRED:
            path = self.backup / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(b"opaque backup input")
            path.chmod(0o600)
            self.manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.save()

    def save(self) -> None:
        path = self.backup / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        path.chmod(0o600)

    def test_verified_manifest_roundtrip(self) -> None:
        self.assertEqual("a" * 40, backupset.verify_backup(self.backup, VERSIONS)["revision"])

    def test_format_one_and_wrong_versions_are_rejected(self) -> None:
        self.manifest["format"] = 1
        self.save()
        with self.assertRaisesRegex(ValueError, "format"):
            backupset.verify_backup(self.backup, VERSIONS)
        self.manifest["format"] = 2
        self.manifest["versions"]["jenkins"] = "jenkins/jenkins:0@sha256:" + "0" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "version"):
            backupset.verify_backup(self.backup, VERSIONS)

    def test_corruption_missing_unlisted_and_escaping_files_are_rejected(self) -> None:
        path = self.backup / backupset.REQUIRED[1]
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            backupset.verify_backup(self.backup, VERSIONS)
        path.unlink()
        with self.assertRaises(ValueError):
            backupset.verify_backup(self.backup, VERSIONS)
        self.setUp()
        del self.manifest["files"][backupset.REQUIRED[0]]
        self.save()
        with self.assertRaises(ValueError):
            backupset.verify_backup(self.backup, VERSIONS)
        self.setUp()
        self.manifest["files"]["../outside"] = "0" * 64
        self.save()
        with self.assertRaises(ValueError):
            backupset.verify_backup(self.backup, VERSIONS)

    def test_images_archive_is_required_only_when_declared(self) -> None:
        self.manifest["includes_images"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "images"):
            backupset.verify_backup(self.backup, VERSIONS)
        images = self.backup / backupset.IMAGES
        images.write_bytes(b"tar")
        images.chmod(0o600)
        self.manifest["files"][backupset.IMAGES] = hashlib.sha256(b"tar").hexdigest()
        self.save()
        self.assertTrue(backupset.verify_backup(self.backup, VERSIONS)["includes_images"])
        self.manifest["includes_images"] = False
        self.save()
        with self.assertRaisesRegex(ValueError, "unexpected"):
            backupset.verify_backup(self.backup, VERSIONS)

    def test_world_readable_and_symlink_inputs_rejected(self) -> None:
        path = self.backup / backupset.REQUIRED[1]
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permission"):
            backupset.verify_backup(self.backup, VERSIONS)
        path.chmod(0o600)
        target = self.base / "elsewhere"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            backupset.verify_backup(self.backup, VERSIONS)

    def test_destination_rules_for_real_and_rehearsal_restores(self) -> None:
        source_root = pathlib.Path(self.manifest["source"]["DEVOPS_LAB_ROOT"])
        occupied = self.base / "occupied"
        occupied.mkdir()
        (occupied / "sentinel").write_text("retain")
        new = self.base / "new"
        # real restore: same identity is fine, overlap and non-empty are not
        backupset.validate_destination(self.manifest, new, "source", "source.test", 443, 2224, rehearsal=False)
        for root in (occupied, source_root, source_root / "nested"):
            with self.assertRaises(ValueError):
                backupset.validate_destination(self.manifest, root, "clone", "clone.test", 8443, 2225, rehearsal=False)
        with self.assertRaisesRegex(ValueError, "different"):
            backupset.validate_destination(self.manifest, new, "source", "clone.test", 8443, 8443, rehearsal=False)
        # rehearsal: every identity element must differ
        backupset.validate_destination(self.manifest, new, "clone", "clone.test", 8443, 2225, rehearsal=True)
        for project, domain, https, ssh in (("source", "clone.test", 8443, 2225), ("clone", "source.test", 8443, 2225),
                                            ("clone", "clone.test", 443, 2225), ("clone", "clone.test", 8443, 2224)):
            with self.assertRaisesRegex(ValueError, "rehearsal"):
                backupset.validate_destination(self.manifest, new, project, domain, https, ssh, rehearsal=True)
        self.assertEqual("retain", (occupied / "sentinel").read_text())
        self.assertFalse(new.exists())

    def test_invalid_identity_raises_value_error(self) -> None:
        new = self.base / "new"
        with self.assertRaises(ValueError):
            backupset.validate_destination(self.manifest, new, "BAD NAME", "clone.test", 8443, 2225, rehearsal=False)
        with self.assertRaises(ValueError):
            backupset.validate_destination(self.manifest, new, "clone", "not a domain", 8443, 2225, rehearsal=False)
        with self.assertRaises(ValueError):
            backupset.validate_destination(self.manifest, new, "clone", "clone.test", 999999, 2225, rehearsal=False)

    def test_identity_from_manifest(self) -> None:
        self.assertEqual({"project": "source", "base_domain": "source.test", "https_port": "443",
                          "ssh_port": "2224", "bind_address": "0.0.0.0"},
                         backupset.identity_from_manifest(self.manifest))

    def test_safe_archive_rejects_paths_links_and_devices(self) -> None:
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("device", tarfile.CHRTYPE)):
            bad = self.base / "bad.tar"
            with tarfile.open(bad, "w") as archive:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = "../escape"
                archive.addfile(info)
            with self.assertRaises(ValueError):
                backupset.validate_tar(bad)
        with tarfile.open(self.base / "good.tar", "w") as archive:
            info = tarfile.TarInfo("nested/file")
            info.size = 2
            archive.addfile(info, io.BytesIO(b"ok"))
        backupset.validate_tar(self.base / "good.tar")

    def test_links_are_allowed_only_when_they_stay_inside_the_archive(self) -> None:
        """GitLab keeps a hashed symlink beside each certificate in /etc/gitlab/trusted-certs."""
        def archive_with(kind, name, linkname):
            path = self.base / "links.tar"
            with tarfile.open(path, "w") as archive:
                certificate = tarfile.TarInfo("./trusted-certs/devops-lab.crt")
                certificate.size = 2
                archive.addfile(certificate, io.BytesIO(b"ok"))
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = linkname
                archive.addfile(info)
            return path

        backupset.validate_tar(archive_with(tarfile.SYMTYPE, "./trusted-certs/5d4de9e7.0", "devops-lab.crt"))
        backupset.validate_tar(archive_with(tarfile.LNKTYPE, "./copy.crt", "./trusted-certs/devops-lab.crt"))
        for kind, name, linkname in ((tarfile.SYMTYPE, "./trusted-certs/x", "../../../etc/shadow"),
                                     (tarfile.SYMTYPE, "./x", "/etc/shadow"),
                                     (tarfile.LNKTYPE, "./x", "../outside")):
            with self.assertRaises(ValueError):
                backupset.validate_tar(archive_with(kind, name, linkname))

    def test_archive_and_extract_round_trip_with_excludes(self) -> None:
        source = self.base / "home"
        (source / "jobs" / "x").mkdir(parents=True)
        (source / "jobs" / "x" / "config.xml").write_text("<x/>")
        (source / "workspace").mkdir()
        (source / "workspace" / "junk").write_text("big")
        (source / "secrets").mkdir()
        (source / "secrets" / "master.key").write_text("k")
        (source / "link").symlink_to("jobs")
        (source / "runtime.env").write_text("A=1\n")
        target = self.base / "home.tar"
        backupset.archive_directory(source, target, excludes=("workspace",))
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        with tarfile.open(target) as archive:
            names = {member.name for member in archive}
        self.assertIn("./jobs/x/config.xml", names)
        self.assertIn("./secrets/master.key", names)
        self.assertNotIn("./workspace/junk", names)
        self.assertNotIn("./link", names)
        out = self.base / "out"
        backupset.extract_tar(target, out, exclude=("runtime.env",))
        self.assertEqual("<x/>", (out / "jobs" / "x" / "config.xml").read_text())
        self.assertFalse((out / "runtime.env").exists())
        self.assertEqual(0o700, out.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
