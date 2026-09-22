# GitLab login

This guide is for administrators who want one sign-in for all three services.

## Enable it

```bash
scripts/lab enable-gitlab-auth
```

The command creates two confidential OAuth applications in GitLab:
`devops-lab-sonarqube`, with redirect `<SONAR_URL>/oauth2/callback/gitlab` and
scope `api`, and `devops-lab-jenkins`, with redirect
`<JENKINS_URL>/securityRealm/finishLogin` and scopes `openid profile email`.
Their secrets are written into the runtime root as
`secrets/sonar_gitlab_oauth_secret` and
`secrets/jenkins/gitlab_oidc_client_secret`, mode 0600.

It then configures SonarQube's GitLab authentication as one document through
the v2 endpoint `/api/v2/dop-translation/gitlab-configurations`, with group
synchronisation, just-in-time provisioning and sign-up on first login. For
Jenkins it writes `config/jenkins/casc.d/50-gitlab-auth.yaml` in the runtime
root: an oic-auth security realm pointed at GitLab's OpenID Connect discovery
document, taking the user name from `nickname` and the groups from the
`groups` claim. Jenkins is then restarted and waited for.

Re-run the command as often as you like. An application whose redirect still
matches and whose secret is still on disk is `kept`, and Jenkins is only
restarted when the overlay or the client actually changed.

## Group mapping

A GitLab group path is the Jenkins group name and the SonarQube group name.
The item and node roles that [`add-team`](teams.md) creates therefore apply to
a person the moment they sign in, with no second membership list to maintain.

## Keeping an administrator

Signing in through GitLab is the normal path: an unauthenticated browser is
sent to `/securityRealm/commenceLogin`, which redirects to GitLab's
authorisation endpoint.

The local `admin` account survives as the escape hatch. Open the sign-in page
directly, `https://jenkins.devops.test/login`, and oic-auth serves a username
and password form that posts to `securityRealm/escapeHatch`. That path is not
a page in its own right — opening it with a GET answers 404 — so go to
`/login`. The credentials are `admin` and the contents of
`secrets/jenkins/jenkins_admin_password`, unchanged by enabling GitLab login.

The same account continues to work with HTTP basic authentication for the API
and for `scripts/lab`, which is how `add-team` and this command keep reaching
Jenkins afterwards. SonarQube keeps its own local `admin` as well.

## Rollback

Jenkins goes back to local sign-in as soon as the overlay is gone: the core
configuration ships no security realm, and the built-in local realm is loaded
again whenever no overlay declares one.

```bash
rm "$DEVOPS_LAB_ROOT/config/jenkins/casc.d/50-gitlab-auth.yaml"
scripts/lab down
scripts/lab up
```

SonarQube is turned off through the same v2 endpoint. Read the configuration
first to learn its id, then disable it. The password travels in a private
`curl` configuration file instead of a command line, and the requests resolve
the canonical name themselves, the way the platform's own checks do, so the
server needs no DNS entry:

```bash
umask 077
config="$(mktemp)"
printf 'user = "admin:%s"\n' "$(cat "$DEVOPS_LAB_ROOT/secrets/sonar_admin_password")" > "$config"
curl --config "$config" --cacert "$DEVOPS_LAB_ROOT/tls/ca.crt" \
  --noproxy '*' --resolve sonar.devops.test:443:127.0.0.1 \
  https://sonar.devops.test/api/v2/dop-translation/gitlab-configurations
curl --config "$config" --cacert "$DEVOPS_LAB_ROOT/tls/ca.crt" \
  --noproxy '*' --resolve sonar.devops.test:443:127.0.0.1 \
  --request PATCH --header 'Content-Type: application/merge-patch+json' \
  --data '{"enabled": false}' \
  https://sonar.devops.test/api/v2/dop-translation/gitlab-configurations/<id>
rm -f "$config"
```

Use your own `HTTPS_PORT` in both the URL and the `--resolve` entry if it is
not 443, and the bind address if it is not `0.0.0.0`.

Delete the two OAuth applications in GitLab afterwards if you want them gone.
Groups, roles, templates and credentials created by `add-team` are untouched
by all of this, so enabling login again later costs one command.

## Your own identity provider

Attach it to GitLab, not to the other two services: Admin Area → Settings →
General, and the OmniAuth providers GitLab supports. Jenkins and SonarQube go
on trusting GitLab, and the group names people already have keep deciding what
they can see.
