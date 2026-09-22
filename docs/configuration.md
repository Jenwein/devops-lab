# Configuration

This guide is for administrators who need to change the platform's identity,
ports, memory limits, image versions or Jenkins plugin set.

## `runtime.env`

`setup` writes `$DEVOPS_LAB_ROOT/config/runtime.env` with mode 0600. It is the
only file that describes this deployment, and every Compose call reads it.

| Key | Default |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | `devops-lab` |
| `DEVOPS_LAB_ROOT` | the resolved runtime root |
| `BASE_DOMAIN` | `devops.test` |
| `GITLAB_HOST`, `JENKINS_HOST`, `SONAR_HOST` | derived: `gitlab.<domain>`, `jenkins.<domain>`, `sonar.<domain>` |
| `GITLAB_URL`, `JENKINS_URL`, `SONAR_URL` | derived from the hosts; the port is appended only when `HTTPS_PORT` is not 443 |
| `BIND_ADDRESS` | `0.0.0.0` |
| `HTTPS_PORT` | `443` |
| `GITLAB_SSH_PORT` | `2224` |
| `DEVOPS_LAB_UID`, `DEVOPS_LAB_GID` | the deploying user, recorded once |
| `EDGE_MEMORY_LIMIT` | `256m` |
| `GITLAB_MEMORY_LIMIT` | `8g` |
| `JENKINS_MEMORY_LIMIT` | `3g` |
| `POSTGRES_MEMORY_LIMIT` | `2g` |
| `SONARQUBE_MEMORY_LIMIT` | `5g` |

## Re-running `setup`

`setup` is idempotent and merges in a fixed order: an explicit flag wins, then
the value already in `runtime.env`, then an environment fallback
(`DEVOPS_LAB_PROJECT`, `DEVOPS_LAB_DOMAIN`, `DEVOPS_LAB_BIND`,
`DEVOPS_LAB_HTTPS_PORT`, `DEVOPS_LAB_SSH_PORT`) for a key the file does not
have yet, then the default. Values you edited by hand are kept, and keys the
platform does not know are copied through untouched.

The flags are `--root`, `--project`, `--base-domain`, `--bind-address` (an
IPv4 address), `--https-port` and `--ssh-port`. The HTTPS and SSH ports must
differ. `--root` defaults to `DEVOPS_LAB_ROOT`, and `scripts/lab` falls back
to `$HOME/devops-lab` when that variable is unset, so export it whenever the
runtime root is somewhere else.

Changing the domain reissues the leaf certificate under the same CA, so
clients that already trust `tls/ca.crt` keep working. Substitute your own
domain for the example:

```bash
scripts/lab setup --base-domain devops.example.com
scripts/lab up
```

If GitLab login is enabled, run `scripts/lab enable-gitlab-auth` once more
afterwards: the OAuth redirect URIs carry the old names.

## Memory

The five limits are plain keys in `runtime.env`. Edit them and run
`scripts/lab up`; Compose recreates the containers whose limits changed. In
practice GitLab needs at least 4 GB to stay healthy, so treat
`GITLAB_MEMORY_LIMIT` as the floor of any reduction.

## Bind address and ports

Use `--bind-address 127.0.0.1` for a host-only test, so nothing is published
beyond the loopback interface. Use `--https-port 8443` when another service
already owns 443; the three service URLs then carry the port, and browsers,
agents and webhooks must use it too.

```bash
scripts/lab setup --bind-address 127.0.0.1 --https-port 8443
```

## Versions

`versions.env` holds the five image references, each as
`name:tag@sha256:digest`. Both `backup` and `restore` require a clean checkout
at a committed revision: `backup` records the revision it ran on and refuses
uncommitted changes to tracked files, and `restore` refuses a set whose
revision or image references are not the ones the checkout has.

An upgrade therefore takes the backup first, from the old commit, while the
tree is still clean:

```bash
scripts/lab backup
```

Then edit the image line in `versions.env`, commit that change, and rebuild:

```bash
scripts/lab up --build
```

The backup you took beforehand is the rollback point, and it restores only
against the commit it was taken from, so keep that commit.

## Jenkins plugins

`config/jenkins/plugins.txt` has two blocks. The `# direct` block is the set
the platform asks for; the `# frozen dependencies` block pins everything the
plugin installer resolves underneath, so a rebuild is reproducible. Add or
bump a plugin in the direct block, refresh the frozen block from a running
controller if its dependencies moved, then rebuild:

```bash
scripts/lab up --build
```

## Images and `up`

`scripts/lab up` never forces a build: Compose builds only an image that is
missing, which is why a second `up` is fast. `config/jenkins/` is baked into
the Jenkins image, so changing it takes `scripts/lab up --build`;
`config/edge/default.conf.template` is bind-mounted, so changing it takes only
`scripts/lab down` and `scripts/lab up`.

If `$DEVOPS_LAB_ROOT/config/images-pinned.json` exists, it pins all five
services to image ids. An air-gapped restore writes it after loading the
archived images, so later `up` runs neither pull nor build. Delete the file to
go back to the references in `versions.env`.

## Wait time

`DEVOPS_LAB_WAIT_SECONDS` bounds how long `up` waits for the containers and
the endpoints to become healthy. The default is 900 seconds, which covers a
first GitLab start on a modest server.
