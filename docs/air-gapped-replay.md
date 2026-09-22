# Air-gapped replay

This guide is for administrators moving the platform to a host that cannot
reach the registry holding the five images.

## On the source host

```bash
scripts/lab backup --include-images
```

The set then also carries `source/images.tar`, which holds the five images the
platform is running — a few gigabytes on top of the usual size. Copy the whole
set directory to the target, preserving its modes (0700 on directories, 0600
on files); it contains the platform's secrets and GitLab's encryption keys.
Everything else about the set is described in
[backup and restore](backup-restore.md).

## On the target host

The target needs the prerequisites the [README](../README.md) lists, all of
them and nothing more. Nothing in the flow below reaches a registry, so
install Docker itself from packages the host already has.

Recover the code from the set's own bundle and check out the revision the
manifest names, because a restore refuses any other revision:

```bash
git clone /srv/recovery/source/code.bundle /srv/devops-platform
cd /srv/devops-platform
git checkout "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' /srv/recovery/manifest.json)"
export DEVOPS_LAB_ROOT=/srv/devops-lab
scripts/lab restore --backup /srv/recovery --root /srv/devops-lab
```

The restore takes its target root from `--root`. Exporting `DEVOPS_LAB_ROOT`
is for everything afterwards: `scripts/lab status`, `scripts/lab up` and
`scripts/lab down` all act on the root that variable names.

## What happens

Every component's SHA-256 is checked against the manifest before anything is
written, so a truncated transfer fails at the start. The archived images are
then loaded and each one verified against the id the manifest recorded, and
the restore writes `<root>/config/images-pinned.json`, pinning all five
services to those ids with `pull_policy: never`. Compose picks that file up
automatically on every later invocation from this root, which is why nothing
is pulled and nothing is built — not during the restore, and not on the
`scripts/lab up` runs that follow it. The one-off root helper container that
unpacks GitLab's configuration uses the set's own Jenkins image for the same
reason.

Delete `config/images-pinned.json` to go back to the registry references in
`versions.env`, for instance once the host has registry access after all.

## What is verified, and what is not

On a connected host, on 2026-09-22, a rehearsal restore from an image-carrying
set ran end to end: for the whole restore window Docker recorded nothing but a
`load` event for each of the five images — no pull, no build, no tag — and the
restore wrote `config/images-pinned.json`, pinning all five services to exactly
those loaded ids with `pull_policy: never`. The evidence file from that run
reported every fence check true and an exact Jenkins plugin match; the unit
suite covers the same pinning without Docker, and with it through a real
Compose render.

The same flow on a host with genuinely no registry access has not been
exercised. Two operations still need one: `scripts/lab up --build`, which
forces a rebuild of the `edge` and `jenkins` images, and a fresh
`scripts/lab setup` followed by `scripts/lab up`, which has no pins to work
from. Only the restore path is air-gap capable.

If you have a maintenance window on the target, rehearse first: restore the
set under a different project, domain and ports with `--rehearsal`, confirm
the evidence file, and only then restore for real.
