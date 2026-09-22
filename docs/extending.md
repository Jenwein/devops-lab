# Extending the platform

This guide is for administrators who want to customise a deployment without
forking the tracked files, so that upstream updates keep merging cleanly.
Plugins, Jenkins overlays and extra secrets all live in the runtime root; only
the last section, adding a service, changes the repository.

## SonarQube plugins

`$DEVOPS_LAB_ROOT/config/sonarqube/plugins/` is mounted into the container as
`/opt/sonarqube/extensions/plugins`. Drop a jar in, restart, and SonarQube
loads it. The platform ships no analysers beyond the ones in the image, so
this is how C and C++ analysis arrives, through the community `sonar-cxx`
plugin.

Take the jar and its published SHA-256 from the plugin's own release page,
check that the release claims support for the SonarQube version pinned in
`versions.env`, and verify the download before restarting:

```bash
cd "$DEVOPS_LAB_ROOT/config/sonarqube/plugins"
curl --fail --location --remote-name "$PLUGIN_URL"
echo "$PLUGIN_SHA256  $(basename "$PLUGIN_URL")" | sha256sum -c -
```

```bash
cd /srv/devops-platform
scripts/lab down
scripts/lab up
```

The first block leaves the shell in the plugin directory, so the second one
returns to the checkout that `scripts/lab` lives in. That plugin directory is
inside `config/`, which a backup archives whole, so the jar travels with the
set and is there again after a restore.

## Jenkins configuration overlays

Any `.yaml` or `.yml` file in `$DEVOPS_LAB_ROOT/config/jenkins/casc.d/` is
loaded after the platform's own Configuration as Code document, with the
override merge strategy: a scalar replaces the one below it, a mapping is
merged key by key, and a sequence is replaced as a whole. That last rule is
the one that bites. The global roles are a sequence, so an overlay that adds
one has to repeat the two the platform ships, or they disappear:

```yaml
jenkins:
  authorizationStrategy:
    roleBased:
      roles:
        global:
          - name: admin
            permissions: [Overall/Administer]
            entries:
              - user: admin
          - name: authenticated
            permissions: [Overall/Read]
            entries:
              - group: authenticated
          - name: auditor
            permissions: [Overall/Read, Job/Read, View/Read]
            entries:
              - group: auditors
```

Overlays take effect on a restart, `scripts/lab down` then `scripts/lab up`.

The security realm is a special case. The core document declares none:
`config/jenkins/casc-local-realm.yaml`, which defines the local `admin`, is
added to the sources only when no overlay declares a `securityRealm` of its
own. Two sources naming a realm would merge into one realm with two entries
and Jenkins would refuse to start. That is exactly how
[`enable-gitlab-auth`](gitlab-login.md) swaps in the GitLab realm, and why
deleting its overlay brings local sign-in back.

## Extra secrets for Jenkins

`$DEVOPS_LAB_ROOT/secrets/jenkins/` is mounted read-only into the container,
and the entrypoint exports every file in it as an environment variable named
after the file: lower case becomes upper case and a dash becomes an
underscore, so `artifactory-token` arrives as `ARTIFACTORY_TOKEN`. An overlay
then refers to it as `${ARTIFACTORY_TOKEN}` — for a credential, a server URL,
anything Configuration as Code can interpolate. Write the file with mode 0600
and no trailing newline, as the platform's own secrets are written, and it
will be in the backup set with them.

## Adding a service

This one is a fork-level change, not a runtime-root one. A sixth service needs
its block in `compose.yaml` — including a `healthcheck:`, because
`scripts/lab status` and the wait in `scripts/lab up` require every service to
report `healthy` — a `<NAME>_IMAGE` line pinned by digest in `versions.env`,
and its name in the `SERVICES` tuple in `scripts/compose.py`; the image key,
the Compose render checks, the backup's image list and the set of containers
that must be healthy all follow from that tuple. A service built from the
repository instead of pulled belongs in `BUILT_SERVICES` in
`scripts/backup.py`, a pulled one in the pull list in `scripts/restore.py`,
and one with an HTTP endpoint worth probing in `CHECKS` in
`scripts/status.py`.

A service that needs its own host name costs more: a server block in
`config/edge/default.conf.template`, the variable in that container's
`NGINX_ENVSUBST_FILTER`, and the name in the certificate, which
`scripts/bootstrap.py` issues for exactly `gitlab`, `jenkins` and `sonar`.
