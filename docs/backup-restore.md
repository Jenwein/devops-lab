# Backup and restore

This guide is for administrators responsible for recovery.

## Take a backup

```bash
scripts/lab backup
scripts/lab backup --include-images
```

A backup runs against a clean checkout at a committed revision, because that
revision is what a restore will be checked against; uncommitted changes to
tracked files stop the run. The platform is quiesced service by service:
Jenkins is put into quiet mode and must be idle, then stopped; SonarQube is
stopped for the database dump; GitLab must have no running jobs and no object
storage configured, and its Puma and Sidekiq are stopped around the supported
`gitlab-backup create`. Everything is started again in a `finally` step, even
when the backup fails, and the command waits until the containers are healthy
and GitLab answers before it returns.

The set lands in `$DEVOPS_LAB_ROOT/backups/<UTC timestamp>/` with mode 0700 on
directories and 0600 on files, and it is verified — every component present,
every checksum matching — before the command reports success.

## What a set contains

`manifest.json` and `checkpoint.json`, `source/code.bundle` with the full git
history, `gitlab/application.tar` and `gitlab/config.tar` (`/etc/gitlab`,
including `gitlab-secrets.json`), `jenkins/home.tar` with `war`, `cache`,
`caches`, `tools`, `workspace`, `logs` and `.cache` left out,
`runtime/secrets.tar`, `runtime/tls.tar`, `runtime/config.tar`,
`sonar/sonar.dump`, and `source/images.tar` only with `--include-images`.

The manifest records the revision, the five image references from
`versions.env`, the id of each running image and the digest of each upstream
one, the installed Jenkins plugin versions, the source identity from
`runtime.env` and a SHA-256 for each component.

A set holds the platform's secrets and GitLab's encryption keys. Treat it like
a credential store.

## Restore for real

Run this on a new host, or on the source host after `scripts/lab down` with a
different, empty `--root`; then export `DEVOPS_LAB_ROOT` to that root so every
later command addresses the restored platform.

```bash
scripts/lab restore --backup /srv/backups/20260922T020000Z --root /srv/devops-lab-restored
```

The identity comes from the manifest; `--project`, `--base-domain`,
`--https-port`, `--ssh-port` and `--bind-address` override it one at a time.
Before anything is written, the restore insists that the checkout is clean and
at the manifest's revision, that the target root is empty and does not overlap
the source root, that both ports are free, and that no Compose project of the
target name exists. On success the platform runs from the new root, the
idempotent initialisation has run, and the evidence is in
`<root>/evidence/restore.json`.

## Rehearse without touching the source

```bash
scripts/lab restore --backup /srv/backups/20260922T020000Z --root /srv/devops-lab-rehearsal \
  --project devops-rehearsal --base-domain rehearsal.test --https-port 9443 --ssh-port 2226 --rehearsal
```

All four identity elements must differ from the source. The clone publishes
HTTPS and SSH on 127.0.0.1 only, its application networks are internal,
GitLab's webhooks are deleted and its runners deactivated after the data is
in, and TCP probes from the four application containers to the three source
URLs and to the internet must all come back blocked. Those four fence checks,
the endpoint checks and an exact match of the Jenkins plugin set are recorded
in the evidence file. The clone's stack is removed afterwards; the target root
stays for inspection, so delete it yourself when you are done with it.

## Schedule it and copy it off the host

```bash
0 2 * * * cd /srv/devops-platform && DEVOPS_LAB_ROOT=/srv/devops-lab scripts/lab backup >> /srv/devops-lab/evidence/backup.log 2>&1
```

```bash
rsync -a --chmod=D700,F600 /srv/devops-lab/backups/ backup-host:/srv/devops-lab-backups/
```

Keep the commit each set was taken from: a restore refuses a set whose
revision or image references are not the ones the checkout has. The
[configuration](configuration.md) guide covers the upgrade order that follows
from that.

## Restoring under a new domain

Certificates are reissued for the new names by the restore itself, under the
CA that came with the set, so clients that already trust `tls/ca.crt` keep
working. Two things outside the runtime root still point at the old names. Run
`scripts/lab enable-gitlab-auth` again if GitLab login was in use, because the
OAuth redirect URIs carry the old host names, and rescan each team's
organization folder so GitLab's project webhooks are registered against the
new Jenkins address.

A target that cannot reach a registry needs
[air-gapped replay](air-gapped-replay.md) instead.
