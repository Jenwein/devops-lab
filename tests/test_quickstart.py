import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
HELLO = REPOSITORY / "examples" / "quickstart" / "hello"
sys.path.insert(0, str(REPOSITORY / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests"))
sys.path.insert(0, str(REPOSITORY / "examples" / "quickstart"))
import quickstart  # noqa: E402
from fakes import FakeTransport  # noqa: E402


class DiscoveredJobTests(unittest.TestCase):
    """The GitLab organization folder names the project after its full path, url-encoded."""

    def resolve(self, jobs):
        lab = mock.Mock()
        lab.jenkins.return_value = FakeTransport({("GET", "/job/demo/job/gitlab/api/json"): (200, {"jobs": jobs})})
        return quickstart.discovered_job(lab)

    def test_encoded_group_path_is_found_and_requoted(self) -> None:
        self.assertEqual("/job/demo/job/gitlab/job/demo%252Fhello",
                         self.resolve([{"name": "demo%2Fhello"}]))

    def test_plain_project_name_is_also_accepted(self) -> None:
        self.assertEqual("/job/demo/job/gitlab/job/hello", self.resolve([{"name": "hello"}]))

    def test_unrelated_projects_are_ignored(self) -> None:
        self.assertIsNone(self.resolve([{"name": "demo%2Fother"}, {"name": "hello-world"}]))

    def test_missing_folder_is_not_an_error(self) -> None:
        lab = mock.Mock()
        lab.jenkins.return_value = FakeTransport({("GET", "/job/demo/job/gitlab/api/json"): (404, {})})
        self.assertIsNone(quickstart.discovered_job(lab))


class WaitBuildTests(unittest.TestCase):
    """A stale failure from an earlier revision must not decide the run."""

    def lab_with(self, builds):
        lab = mock.Mock()
        lab.jenkins.return_value = FakeTransport({
            ("GET", "/job/demo/job/gitlab/api/json"): (200, {"jobs": [{"name": "demo%2Fhello"}]}),
            ("GET", "/job/demo/job/gitlab/job/demo%252Fhello/job/main/api/json"): (200, {"builds": builds}),
        })
        return lab

    @staticmethod
    def build(number, result, sha):
        return {"number": number, "result": result, "url": f"https://j/{number}/",
                "actions": [{}, {"_class": "hudson.plugins.git.util.BuildData",
                                 "lastBuiltRevision": {"SHA1": sha}}]}

    def test_older_failure_is_skipped_for_the_pushed_revision(self) -> None:
        lab = self.lab_with([self.build(2, "SUCCESS", "new"), self.build(1, "FAILURE", "old")])
        self.assertEqual(2, quickstart.wait_build(lab, "new", timeout=1)["number"])

    def test_failure_of_the_pushed_revision_is_reported(self) -> None:
        lab = self.lab_with([self.build(2, "FAILURE", "new"), self.build(1, "SUCCESS", "old")])
        with self.assertRaises(RuntimeError) as raised:
            quickstart.wait_build(lab, "new", timeout=1)
        self.assertIn("FAILURE", str(raised.exception))

    def test_unfinished_build_times_out_rather_than_passing(self) -> None:
        lab = self.lab_with([self.build(2, None, "new")])
        with self.assertRaises(RuntimeError) as raised:
            quickstart.wait_build(lab, "new", timeout=0)
        self.assertIn("no finished build", str(raised.exception))


class GitLabProjectTests(unittest.TestCase):
    def lab_with(self, lookup):
        lab = mock.Mock()
        lab.gitlab.return_value = FakeTransport({
            ("GET", "/projects/demo%2Fhello"): lookup,
            ("GET", "/groups"): (200, [{"full_path": "demo", "id": 3}]),
            ("POST", "/projects"): (201, {"id": 9, "path_with_namespace": "demo/hello"}),
        })
        return lab

    def test_existing_project_is_reused(self) -> None:
        lab = self.lab_with((200, {"id": 4}))
        self.assertEqual(4, quickstart.ensure_gitlab_project(lab)["id"])

    def test_redirect_left_by_a_scheduled_deletion_creates_the_project(self) -> None:
        """GitLab renames a project it is about to delete and leaves a 301 on the old path."""
        lab = self.lab_with((301, {}))
        self.assertEqual(9, quickstart.ensure_gitlab_project(lab)["id"])


class AgentProjectTests(unittest.TestCase):
    """The agent must not land in the platform's own Compose project."""

    def lab(self):
        lab = mock.Mock()
        lab.project = "devops-lab"
        lab.repo = REPOSITORY
        lab.root = pathlib.Path("/srv/devops-lab")
        return lab

    def test_agent_runs_in_its_own_project(self) -> None:
        argv = quickstart.agent_compose(self.lab())
        self.assertIn("-p", argv)
        self.assertEqual("devops-lab-quickstart", argv[argv.index("-p") + 1])
        self.assertIn(str(pathlib.Path("/srv/devops-lab/config/runtime.env")), argv)

    def test_agent_is_started_with_the_authoritative_environment(self) -> None:
        lab = self.lab()
        with mock.patch.object(quickstart, "compose_environment", return_value={"X": "1"}) as environment:
            quickstart.start_agent(lab)
        environment.assert_called_once_with(lab.repo, lab.root)
        self.assertEqual({"X": "1", "PLATFORM_PROJECT": "devops-lab"}, lab.run.call_args.kwargs["env"])
        self.assertEqual("-p", lab.run.call_args.args[0][2])

    def test_stop_takes_the_agent_project_down(self) -> None:
        """The agent's file lives under examples/, so a bare `docker compose down` cannot find it."""
        lab = self.lab()
        with mock.patch.object(quickstart, "Lab", return_value=lab), \
                mock.patch.object(quickstart, "compose_environment", return_value={"X": "1"}):
            self.assertEqual(0, quickstart.main(["--root", str(lab.root), "--stop"]))
        argv = lab.run.call_args.args[0]
        self.assertEqual("down", argv[-1])
        self.assertEqual("devops-lab-quickstart", argv[argv.index("-p") + 1])
        self.assertEqual({"X": "1", "PLATFORM_PROJECT": "devops-lab"}, lab.run.call_args.kwargs["env"])
        lab.groovy.assert_not_called()

    def test_agent_file_names_the_platform_without_compose_project_name(self) -> None:
        """-p rewrites COMPOSE_PROJECT_NAME for interpolation, so the network would not resolve."""
        text = (REPOSITORY / "examples" / "agents" / "linux" / "compose.yaml").read_text()
        self.assertNotIn("${COMPOSE_PROJECT_NAME}", text)
        self.assertIn("${PLATFORM_PROJECT:?PLATFORM_PROJECT is required}_edge", text)


class PushScriptTests(unittest.TestCase):
    """The push runs against a real local repository; only the transport differs from a live run."""

    def push(self, work: pathlib.Path, remote: pathlib.Path, extra: str | None = None) -> str:
        work.mkdir()
        quickstart.prepare_sample(work / "src")
        if extra is not None:
            (work / "src" / "extra.txt").write_text(extra)
        environment = {
            "PATH": os.environ["PATH"], "HOME": str(work), "WORK": str(work), "REMOTE": str(remote),
            "GIT_AUTHOR_NAME": "quickstart", "GIT_AUTHOR_EMAIL": "quickstart@devops.invalid",
            "GIT_COMMITTER_NAME": "quickstart", "GIT_COMMITTER_EMAIL": "quickstart@devops.invalid",
            "GIT_AUTHOR_DATE": quickstart.SAMPLE_DATE, "GIT_COMMITTER_DATE": quickstart.SAMPLE_DATE,
        }
        result = subprocess.run(["sh", "-eu", "-c", quickstart.PUSH_SCRIPT], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return (work / "sha").read_text().strip()

    def remote_head(self, remote: pathlib.Path) -> str:
        return subprocess.run(["git", "-C", str(remote), "rev-parse", "main"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_unchanged_sample_pushes_nothing_and_a_change_fast_forwards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

            first = self.push(root / "one", remote)
            self.assertEqual(first, self.remote_head(remote))
            tracked = subprocess.run(["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
                                     capture_output=True, text=True, check=True).stdout.split()
            self.assertIn("Jenkinsfile", tracked)
            self.assertFalse([name for name in tracked if "__pycache__" in name or name.endswith(".pyc")])

            self.assertEqual(first, self.push(root / "two", remote))
            self.assertEqual(first, self.remote_head(remote))

            third = self.push(root / "three", remote, extra="changed\n")
            self.assertNotEqual(first, third)
            self.assertEqual(third, self.remote_head(remote))
            parent = subprocess.run(["git", "-C", str(remote), "rev-parse", "main^"],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(first, parent)


class SampleProjectTests(unittest.TestCase):
    def test_sample_tests_pass_on_their_own(self) -> None:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HELLO), "-p", "test_*.py"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_pipeline_and_scanner_settings_match_the_team_contract(self) -> None:
        jenkinsfile = (HELLO / "Jenkinsfile").read_text()
        self.assertIn("label 'demo-linux'", jenkinsfile)
        self.assertIn("withSonarQubeEnv(installationName: 'platform', credentialsId: 'sonar-demo')", jenkinsfile)
        self.assertIn("waitForQualityGate abortPipeline: true", jenkinsfile)
        properties = (HELLO / "sonar-project.properties").read_text()
        self.assertIn("sonar.projectKey=demo-hello\n", properties)

    def test_agent_image_is_pinned_and_verifies_scanner_checksum(self) -> None:
        dockerfile = (REPOSITORY / "examples" / "agents" / "linux" / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r"FROM eclipse-temurin:21-jre@sha256:[0-9a-f]{64}")
        self.assertIn("sha256sum -c", dockerfile)
        entrypoint = (REPOSITORY / "examples" / "agents" / "linux" / "entrypoint.sh").read_text()
        self.assertIn("-webSocket", entrypoint)
        self.assertIn("-secret @/run/agent/secret", entrypoint)
        # args4j expands a leading @ into one argument per line, which breaks a multi-line PEM.
        self.assertIn('-cert "$(cat /run/tls/ca.crt)"', entrypoint)
        self.assertNotIn("-cert @", entrypoint)
        # git and curl cannot use the Java truststore, so they need a bundle of their own.
        self.assertIn("export GIT_SSL_CAINFO=", entrypoint)
        self.assertIn("export CURL_CA_BUNDLE=", entrypoint)
        self.assertIn("/run/tls/ca.crt > ", entrypoint)
        # SonarScanner 7 reads only its own keystore under SONAR_USER_HOME.
        self.assertIn('export SONAR_USER_HOME="${HOME}/.sonar"', entrypoint)
        self.assertIn('${SONAR_USER_HOME}/ssl/truststore.p12" -storepass changeit\n', entrypoint)
        self.assertNotIn("SONAR_SCANNER_JAVA_OPTS", entrypoint)
        self.assertEqual(0, subprocess.run(["bash", "-n", str(REPOSITORY / "examples" / "quickstart" / "run.sh")]).returncode)


if __name__ == "__main__":
    unittest.main()
