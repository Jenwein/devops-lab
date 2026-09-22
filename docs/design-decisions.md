# Design decisions

This guide is for administrators and forks who want the reasoning behind the
choices that shape the platform, and what each one costs.

**No sudo, no added capabilities, no fixed UID.** Any user in the `docker`
group can deploy, on a server whose accounts you do not control. The cost is
that root-owned file trees, GitLab's and PostgreSQL's, are reachable only
through containers, and a restore has to unpack GitLab's configuration with a
one-off root helper container.

**Jenkins and SonarQube run as the deploying user.** Their data then belongs
to a real account that can read, archive and restore it with ordinary tools.
The cost is that SonarQube still needs group 0, because its image insists on
it, and moving a runtime root to another user means re-owning the tree first.

**The environment files outrank the shell.** `versions.env` and `runtime.env`
are re-applied over the environment on every Compose call, so the files decide
which stack a command touches. The cost is that `scripts/lab` cannot be
steered with exported Compose variables. That is deliberate: a stale export
once pointed a rehearsal restore at the live platform's data.

**Images are optional in a backup set.** The pinned digests in `versions.env`
reproduce them from a registry, so a normal set stays small; `--include-images`
archives the five running images for an air-gapped target. The cost is that
the choice belongs to backup time, not restore time.

**A restore is real by default.** Rehearsal is the `--rehearsal` flag, which
demands a different identity and runs the clone on a fenced network. The cost
is that a real restore on the source host needs the source stopped first; the
pre-flight checks on ports, project name and destination exist so that misuse
fails before anything is written.

**C and C++ analysis is an optional SonarQube plugin.** The platform ships
none; the deployer drops a jar into the runtime root's
`config/sonarqube/plugins/`, which is mounted into the container. The cost is
that the plugin's compatibility with the pinned SonarQube version is the
deployer's to verify.

**Administrators create agents.** Role Strategy checks agent creation against
Jenkins itself, not against a node that does not exist yet, so a node role's
name pattern cannot restrict who creates one. The cost is one administrator
step per agent; everything afterwards, connecting, configuring and building,
is the team's.

**Team access is folder- and prefix-based.** A team gets a Jenkins folder, an
item role over it and a node role over agents whose names start with the team
name. The cost is that labels are shared: a team can target another team's
agent by label unless the naming convention is respected.

**Module names avoid shadowing the standard library.** `scripts/` goes on
`sys.path` when the scripts import each other, so the modules are called
`transport.py` and `platform_init.py` rather than the obvious names. It reads
oddly until you know why.

**Provisioning is out of scope.** There is no Ansible role, no Terraform
module and no installer. `scripts/lab` is the interface a provisioning tool
calls, which keeps the platform testable on its own and free of a second
configuration language.

**GitLab is the identity hub.** Jenkins and SonarQube trust GitLab rather than
an external directory, so an organisation attaches its own identity provider
to GitLab once and both other services follow. The cost is that GitLab is a
hard dependency of signing in anywhere.
