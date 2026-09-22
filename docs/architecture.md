# Architecture

This guide is for administrators who want to know what runs where, and why the
platform is safe to run without privileges on the host.

## Services and networks

Five containers make up the platform: `edge` (nginx), `gitlab` (GitLab CE),
`jenkins` (the controller), `sonarqube` and `postgres`, the database SonarQube
uses.

They sit on two Compose networks. `edge` joins `edge`, `gitlab`, `jenkins` and
`sonarqube`. `sonar-db` is declared `internal: true` and joins only `postgres`
and `sonarqube`, so the database has no route out and no other service can
reach it.

On the `edge` network the edge container carries the three canonical names —
`gitlab.<domain>`, `jenkins.<domain>` and `sonar.<domain>` — as network
aliases. A container that asks for `sonar.devops.test` therefore reaches the
edge and gets the same certificate and the same URL a browser gets. There is
no second, internal set of names to keep in step.

Two ports are published: `${BIND_ADDRESS}:${HTTPS_PORT}` on the edge and
`${BIND_ADDRESS}:${GITLAB_SSH_PORT}` for GitLab SSH. The service ports
themselves stay on the Compose networks.

## TLS

`setup` creates a private CA, `tls/ca.crt` with its key `tls/ca.key`, valid for
ten years. It then issues one leaf certificate for the three names,
`tls/server.crt`, valid for 825 days. On every later run `setup` verifies the
leaf against the CA for each name and reissues it only when a name no longer
verifies, which is what a domain change causes. The CA itself is never
replaced.

Java needs its own trust store. The first `scripts/lab up` builds
`tls/trust/java-cacerts.p12` once, by copying the JDK trust store out of the
Jenkins image and importing the CA into it; Jenkins and SonarQube mount that
file read-only and point their JVMs at it. GitLab trusts the CA a third way:
its start command installs `ca.crt` into `/etc/gitlab/trusted-certs` before
the omnibus initialisation runs.

## Run identities

Jenkins runs as `${DEVOPS_LAB_UID}:${DEVOPS_LAB_GID}`, the user who ran
`setup`. SonarQube runs as `${DEVOPS_LAB_UID}:0`, because its image requires
group 0. GitLab runs as root inside its container, as the omnibus package
requires, and PostgreSQL runs as its image's own user.

That split decides what the scripts can touch directly. Jenkins home,
SonarQube data, the secrets and the TLS material belong to the deploying user,
who can read and archive them with ordinary tools. `setup` creates every data
directory as that user, but what GitLab and PostgreSQL then write inside
`data/gitlab` and `data/postgres` is owned by root and by the PostgreSQL image
user, so the scripts reach that content only through the containers.

## Secrets

`setup` generates five secrets:

- `secrets/gitlab_root_password`
- `secrets/sonar_db_password`
- `secrets/sonar_admin_password`
- `secrets/jenkins/jenkins_admin_password`
- `secrets/jenkins/gitlab_webhook_secret`

Initialisation adds `secrets/gitlab_root_token`, the GitLab API token the
scripts work with. `scripts/lab enable-gitlab-auth` adds two more:
`secrets/sonar_gitlab_oauth_secret` and
`secrets/jenkins/gitlab_oidc_client_secret`. Every file is mode 0600 and
carries no trailing newline, so its content can be used as read.

The `secrets/jenkins/` directory is mounted read-only into the Jenkins
container. Its entrypoint exports each file as an environment variable named
after the file, so `jenkins_admin_password` becomes `JENKINS_ADMIN_PASSWORD`,
and Configuration as Code reads the variables. No secret is written into a
configuration file or a command line.

## No sudo, no DNS on the host

Every privileged file operation happens inside a container, so membership of
the `docker` group is the only privilege the platform asks for.

Host-side scripts do not need the server to resolve the canonical names
either. They connect to the bind address and validate the certificate for the
canonical name: `curl --resolve` for the status checks, a fixed-address HTTPS
connection with the CA pinned for the API clients. Resolving the names is a
client concern.

Both Compose environment files, `versions.env` from the checkout and
`config/runtime.env` from the runtime root, are re-applied over the shell
environment on every Compose call. Compose otherwise lets ambient values win
over `--env-file`, and an exported `DEVOPS_LAB_ROOT` or
`COMPOSE_PROJECT_NAME` could then point a command at another stack's data.

## Jenkins authorization

The controller uses Role Strategy. Two global roles ship with it: `admin`
holds Overall/Administer and is assigned to the `admin` user, and
`authenticated` holds Overall/Read, so a signed-in user sees the dashboard and
nothing more until a team role grants it. `scripts/lab add-team` adds one item
role and one node role for each team.

The inbound TCP agent port is disabled, so agents connect over WebSocket
through the HTTPS edge: one open port, one certificate, no second listener.
