# Security

Secrets (service passwords, API tokens, the private CA key, OAuth client
secrets) live under `${DEVOPS_LAB_ROOT}/secrets/` and `${DEVOPS_LAB_ROOT}/tls/`
with mode `0600`, outside the repository. Backup sets contain them too; keep
sets at `0700`/`0600` and transfer them over a private channel.

The default bind address is `0.0.0.0`: the HTTPS port and the GitLab SSH port
are reachable from every interface unless the host firewall restricts them.
Set `--bind-address` at `setup` time or firewall the host before the first `up`.

Report a vulnerability through GitHub's private vulnerability reporting on
this repository rather than a public issue.
