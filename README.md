# devops-lab

[![check](https://github.com/Jenwein/devops-lab/actions/workflows/check.yml/badge.svg)](https://github.com/Jenwein/devops-lab/actions/workflows/check.yml)

A self-hosted DevOps platform for an organisation: GitLab CE, a Jenkins
controller and SonarQube Community behind one HTTPS entry point, deployed with
Docker Compose from this repository. Clone it onto a Linux server, run three
commands, and give each team its own space with one more. Fork it to keep your
own settings: everything site-specific lives outside the tracked files, so
upstream updates merge cleanly.

This page is for the administrator who installs and operates the platform.

## What you get

| Service | Role |
| --- | --- |
| `edge` | nginx: TLS termination and reverse proxy for the three names below |
| `gitlab` | GitLab CE: repositories, groups, tokens, webhooks |
| `jenkins` | Jenkins LTS controller with zero executors; teams connect their own agents |
| `sonarqube` | SonarQube Community with its own `postgres` |

Image versions are pinned by digest in `versions.env`.

## Prerequisites

- A Linux x86_64 server with Docker Engine and the Compose plugin (v2.24 or
  newer), Python 3.12 or newer, git, openssl and curl.
- `vm.max_map_count` of at least 524288 for SonarQube.
- A user in the `docker` group. No sudo, no root, no particular UID.
- DNS or hosts entries that point `gitlab.<domain>`, `jenkins.<domain>` and
  `sonar.<domain>` at the server. The server itself needs none.
- Memory: the default limits add up to about 18 GB; see
  [configuration](docs/configuration.md) to lower them.

## Deploy

```bash
git clone https://github.com/Jenwein/devops-lab.git /srv/devops-platform
cd /srv/devops-platform
export DEVOPS_LAB_ROOT=/srv/devops-lab
scripts/lab setup --base-domain devops.test
scripts/lab up
```

Both directories must already exist, be empty and belong to the deploying
user. On a fresh server the two steps an administrator takes as root are
setting `vm.max_map_count` and creating those directories; nothing else needs
privileges.

`setup` creates the runtime root with a private CA, a TLS certificate,
generated secrets and `runtime.env`; running it again keeps what exists. `up`
builds the two local images, starts the five services, waits for them to be
healthy and runs an idempotent initialisation. The first `up` takes several
minutes while GitLab initialises. Check on it any time with:

```bash
scripts/lab status
```

## Access

| Service | URL | Account | Password file |
| --- | --- | --- | --- |
| GitLab | `https://gitlab.devops.test` | `root` | `secrets/gitlab_root_password` |
| Jenkins | `https://jenkins.devops.test` | `admin` | `secrets/jenkins/jenkins_admin_password` |
| SonarQube | `https://sonar.devops.test` | `admin` | `secrets/sonar_admin_password` |

Paths are relative to `$DEVOPS_LAB_ROOT`. Browsers and agents must trust
`$DEVOPS_LAB_ROOT/tls/ca.crt`.

## Next steps

- Give a team its space in all three services: `scripts/lab add-team <name>`,
  see [teams](docs/teams.md).
- Let people sign in everywhere with their GitLab account:
  `scripts/lab enable-gitlab-auth`, see [GitLab login](docs/gitlab-login.md).
- Prove the whole chain with a sample project and an agent:
  `examples/quickstart/run.sh`, see [quickstart example](docs/quickstart-example.md).
- Take a backup you can restore elsewhere: `scripts/lab backup`, see
  [backup and restore](docs/backup-restore.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Teams](docs/teams.md)
- [GitLab login](docs/gitlab-login.md)
- [Backup and restore](docs/backup-restore.md)
- [Air-gapped replay](docs/air-gapped-replay.md)
- [Extending the platform](docs/extending.md)
- [Quickstart example](docs/quickstart-example.md)
- [Verification](docs/verification.md)
- [Design decisions](docs/design-decisions.md)

## Licence

MIT, see [LICENSE](LICENSE).
