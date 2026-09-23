# Verification

This guide is for contributors changing the code and for administrators
accepting a deployment.

## Unit tests

```bash
python3 -m unittest discover -s tests -p 'test*.py'
```

The suite runs without a platform and covers the parts that can be checked in
isolation: the bootstrap and the permissions it sets; the Compose command and,
where Docker is present, a real render of `compose.yaml`; the HTTPS transport
against a local TLS server with its own throwaway CA; backup-set verification
with real tar files, including the unsafe ones it must reject; the order in
which a backup captures the services; restore validation and the rehearsal
overlay; container status parsing; the request traffic of `add-team`,
`enable-gitlab-auth` and the platform initialisation against fake transports;
the Jenkins entrypoint executed under `sh`; `scripts/lab` executed under
`bash`; the quickstart push against a real bare repository; and this
documentation, whose links, commands and forbidden strings are checked by
`tests/test_docs.py`.

Run it twice, once in a clean shell and once with a conflicting runtime root
exported. The second run proves that the environment files, not the ambient
variables, decide which stack a command touches:

```bash
DEVOPS_LAB_ROOT=/nonexistent/ambient COMPOSE_PROJECT_NAME=ambient \
  python3 -m unittest discover -s tests -p 'test*.py'
```

## Static checks

```bash
python3 -m compileall -q scripts examples tests
```

The five shell files — `scripts/lab`, `config/jenkins/jenkins-entrypoint.sh`,
`config/jenkins/validate-origin.sh`, `examples/agents/linux/entrypoint.sh` and
`examples/quickstart/run.sh` — are checked with `bash -n` and shellcheck. A
Compose render against a temporary runtime root confirms that the five
services appear and that the three upstream images resolve to the references
in `versions.env`.

## Continuous integration

The `check` workflow runs the unit tests in both modes, the compile step, the
shell checks and the Compose render on every push and pull request. The
`images` workflow builds the Jenkins image with its frozen plugin set and the
quickstart agent image once a week and on demand, which catches a plugin or
base image that has disappeared.

The platform itself is not started in CI. GitLab, Jenkins and SonarQube
together need more memory than a hosted runner has, so the end-to-end proof
belongs on a real host.

## Acceptance on a real host

Work through this list on the target server before handing the platform over.

1. On a clean server run `scripts/lab setup`, `scripts/lab up` and
   `scripts/lab status`. Every container is healthy and the three endpoints
   answer.
2. Run `scripts/lab up` a second time. The initialisation reports every step
   as `kept`, and no container is recreated.
3. Run `examples/quickstart/run.sh`. It exits 0 and
   `$DEVOPS_LAB_ROOT/evidence/quickstart.json` shows `build_result: SUCCESS`,
   `gitlab_commit_status: success` and `sonar_quality_gate: OK`.
4. Run it again. It exits 0 and creates no duplicate team, project or agent.
5. Create a GitLab user, then run `scripts/lab add-team demo --member <user>`
   and `scripts/lab enable-gitlab-auth`. Sign in to Jenkins and SonarQube with
   that user through GitLab and confirm it sees the team's folder and project
   and nothing else. Restart Jenkins (`scripts/lab down`, `scripts/lab up`)
   and confirm the same user still sees the folder: team roles must survive
   a restart.

Then take a backup and rehearse a restore from it under a different identity:

```bash
scripts/lab backup
scripts/lab restore --backup <set> --root <new root> --project <other> \
  --base-domain <other> --https-port <other> --ssh-port <other> --rehearsal
```

The rehearsal must finish with every fence check true in
`<new root>/evidence/restore.json`. It removes its own stack afterwards and
leaves the root for inspection.

Finally prove a real restore. Stop the platform, restore the same set into a
fresh root, and check the result against that root:

```bash
scripts/lab down
scripts/lab restore --backup <set> --root <new root>
export DEVOPS_LAB_ROOT=<new root>
scripts/lab status
```

Record the outcome wherever your organisation keeps acceptance evidence; the
platform writes its own under `$DEVOPS_LAB_ROOT/evidence/`.
