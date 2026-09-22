# Quickstart example

This guide is for anyone who wants proof that a deployment works end to end,
from a push to a quality gate.

## Run it

```bash
examples/quickstart/run.sh
```

It needs a platform already running on the same host, and network access the
first time, to build the agent image: that pulls a base image and downloads
the scanner. It takes its runtime root from `DEVOPS_LAB_ROOT` and refuses to
start when that root holds no `runtime.env`.

## What it does

In order: creates team `demo` exactly as [`add-team`](teams.md) would;
creates the inbound WebSocket node `demo-linux` with one executor and the
label `demo-linux`, and stores its secret in the runtime root; builds and
starts the agent from `examples/agents/linux/` as a Compose project of its
own, `<platform project>-quickstart`, attached to the platform's edge network;
waits for the node to come online; creates SonarQube project `demo-hello` and
GitLab project `demo/hello`; pushes the sample under
`examples/quickstart/hello/` from a one-off container on that same network, so
the host itself needs no DNS and no credentials on a command line; triggers a
scan of the organization folder; waits for the `main` branch job of the
discovered project to finish; then checks that the GitLab commit status for
the pushed revision is `success` and that the SonarQube quality gate is `OK`.

The result is written to `$DEVOPS_LAB_ROOT/evidence/quickstart.json`, printed,
and the command exits non-zero if either check failed. A build that finishes
with anything but `SUCCESS` stops the run with the build's URL.

The sample is a three-stage pipeline: unit tests, `sonar-scanner` under
`withSonarQubeEnv`, and `waitForQualityGate`. It is a working model for a team
pipeline, and short enough to read in a minute.

Everything is idempotent. A second run creates no second team, project or
node, pushes nothing when the sample already matches the remote, and exits 0
on the build that is already there.

## The agent

The image is a Temurin 21 JRE pinned by digest, with `sonar-scanner-cli`
7.2.0.5079 installed after a checksum check, plus `git`, `python3` and `curl`.
The container runs as the deploying user, downloads `agent.jar` from the
controller at start and connects inbound over WebSocket through the HTTPS
edge, which is the only way in: the controller's inbound TCP port is disabled.

It trusts the platform CA and nothing else for the controller connection. For
build steps it gets a private bundle of the system roots with the platform CA
appended, and a separate keystore under `SONAR_USER_HOME` because
SonarScanner 7 builds its own SSL context. Copy the directory when you write a
real agent; the trust handling is the part worth keeping.

## Stop it

```bash
examples/quickstart/run.sh --stop
```

That removes the agent's Compose project and nothing else. The team, the two
projects, the node and the evidence file stay, which is what you want for an
acceptance record. Delete them through the services' own interfaces if you
want the platform back as it was.

## If it fails

`scripts/lab status` first: the run assumes five healthy containers and three
answering endpoints. After that the usual suspects are the node not
connecting, which the agent container's log shows, and the organization folder
not discovering the project, which the folder's scan log shows. The
[verification](verification.md) guide puts this run in the wider acceptance
list.
