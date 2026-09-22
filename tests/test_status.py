import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
SPEC = importlib.util.spec_from_file_location("lab_status", REPOSITORY / "scripts" / "status.py")
assert SPEC and SPEC.loader
STATUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATUS)


class StatusTests(unittest.TestCase):
    @staticmethod
    def healthy_rows() -> list[dict[str, str]]:
        return [
            {"Service": service, "State": "running", "Health": "healthy"}
            for service in ("edge", "gitlab", "jenkins", "postgres", "sonarqube")
        ]

    def test_parse_compose_rows_accepts_json_lines(self) -> None:
        payload = '{"Service":"edge","State":"running"}\n{"Service":"gitlab","State":"running"}\n'
        rows = STATUS.parse_compose_rows(payload)
        self.assertEqual([row["Service"] for row in rows], ["edge", "gitlab"])

    def test_endpoint_resolves_to_address_and_bypasses_ambient_proxy(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="403", stderr="")
        with mock.patch.object(STATUS.subprocess, "run", return_value=completed) as run:
            ok, detail = STATUS.endpoint(
                pathlib.Path("/runtime"), "gitlab.devops.test", "443", "/users/sign_in", "10.0.0.5"
            )
        self.assertFalse(ok)
        self.assertEqual("403", detail)
        command = run.call_args.args[0]
        self.assertEqual("*", command[command.index("--noproxy") + 1])
        self.assertEqual("gitlab.devops.test:443:10.0.0.5", command[command.index("--resolve") + 1])

    def test_endpoint_accepts_any_expected_code(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="302", stderr="")
        with mock.patch.object(STATUS.subprocess, "run", return_value=completed):
            ok, _ = STATUS.endpoint(
                pathlib.Path("/runtime"), "jenkins.devops.test", "443", "/login", expected=("200", "302")
            )
        self.assertTrue(ok)

    def test_resolve_address_uses_loopback_for_wildcard_bind(self) -> None:
        self.assertEqual("127.0.0.1", STATUS.resolve_address({"BIND_ADDRESS": "0.0.0.0"}))
        self.assertEqual("10.0.0.5", STATUS.resolve_address({"BIND_ADDRESS": "10.0.0.5"}))
        self.assertEqual("127.0.0.1", STATUS.resolve_address({}))

    def test_containers_accept_the_five_core_services(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(self.healthy_rows()), stderr="")
        with mock.patch.object(STATUS, "compose_ps", return_value=completed) as compose_ps:
            ready, detail = STATUS.containers_ready(pathlib.Path("/repo"), pathlib.Path("/runtime"))
        self.assertTrue(ready)
        self.assertEqual("all containers healthy", detail)
        self.assertEqual((pathlib.Path("/repo"), pathlib.Path("/runtime")), compose_ps.call_args.args)

    def test_containers_reject_stopped_unhealthy_and_missing_services(self) -> None:
        for state, health, expected in (
            ("exited", "", "jenkins=exited/none"),
            ("running", "unhealthy", "jenkins=running/unhealthy"),
        ):
            with self.subTest(state=state, health=health):
                rows = self.healthy_rows()
                rows[2] = {"Service": "jenkins", "State": state, "Health": health}
                completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
                with mock.patch.object(STATUS, "compose_ps", return_value=completed):
                    ready, detail = STATUS.containers_ready(pathlib.Path("/repo"), pathlib.Path("/runtime"))
                self.assertFalse(ready)
                self.assertEqual(expected, detail)
        rows = self.healthy_rows()[:-1]
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
        with mock.patch.object(STATUS, "compose_ps", return_value=completed):
            ready, detail = STATUS.containers_ready(pathlib.Path("/repo"), pathlib.Path("/runtime"))
        self.assertFalse(ready)
        self.assertEqual("missing=sonarqube", detail)


if __name__ == "__main__":
    unittest.main()
