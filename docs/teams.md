# Teams

This guide is for administrators onboarding a team and for the team leads who
work in the space that onboarding creates.

## One command

```bash
scripts/lab add-team payments
scripts/lab add-team payments --member alice --member bob
scripts/lab add-team payments --rotate-tokens
```

The name must match `^[a-z][a-z0-9-]{1,30}$`; it becomes the group, folder,
role and template name in all three services, so pick it once and keep it.

## What it creates

| Service | Objects |
| --- | --- |
| GitLab | Private group `payments`, and a group access token named `jenkins` with scopes `api` and `read_repository`, at Maintainer level, valid for 365 days |
| Jenkins | Folder `payments`, an item role and a node role both named `payments` and both bound to the Jenkins group `payments`, folder credentials `gitlab-payments` and `sonar-payments`, and an organization folder `payments/gitlab` that scans the GitLab group |
| SonarQube | Group `payments`, a permission template `payments` for project keys matching `^payments[-_:].*`, and a global analysis token `jenkins-payments` |

The item role matches `^payments(/.*)?$` and carries the job, run, SCM tag,
credentials and view permissions; the node role matches `^payments-.*$` and
carries build, configure, connect, delete and disconnect. The permission
template gives the SonarQube group user, codeviewer, issueadmin,
securityhotspotadmin, scan and admin on every project key it matches.

Every step prints one `step: outcome` line. For the objects above the outcome
is `kept`, `created`, `updated` or `rotated`; the last two lines name the
members and say what happened to each of them in SonarQube. The command is
idempotent: run it again after a failure, or whenever you add members.
Nothing is ever deleted, and there is no `remove-team`.

## Members

`--member` is repeatable and takes an existing GitLab username; an unknown one
stops the run with `add-team failed: GitLab user 'alice' does not exist;
create it first`. Each member joins the GitLab group as a Developer and is
assigned both Jenkins roles by name. SonarQube only knows a person once they
have signed in, so a member who never has is reported as
`sonar_members: alice=pending-first-login` and joins the SonarQube group on
their first sign-in through GitLab; adding them again later also works.

With [GitLab login](gitlab-login.md) enabled, GitLab group membership alone is
enough: the group path is the Jenkins group and the SonarQube group, so the
roles above apply to whoever is in it.

## Tokens

Both tokens are kept as they are unless you pass `--rotate-tokens`. One
exception is automatic: if the Jenkins folder credential that should hold a
token has gone missing, the token is recreated — or created, if there was none
— so that the credential can be written again. The GitLab group token expires
after 365 days, so schedule a `--rotate-tokens` run inside that year.

## Working in the space

Create a repository in the `payments` group with a `Jenkinsfile` at its root.
The organization folder finds it on its daily scan; to see it sooner, force
one from "Scan GitLab Group Now" in the folder, or by posting to
`/job/payments/job/gitlab/build`. Jenkins registers the project's webhook as
part of discovering it — there is no group-wide system hook — so a brand-new
repository needs that first scan before pushes start triggering builds.

Pin work to the team's own agents with labels that carry the team prefix, as
in `agent { label 'payments-linux' }`, and give SonarQube project keys the same
prefix so the permission template matches. Analysis runs through the shared
installation and the folder's own token:

```groovy
withSonarQubeEnv(installationName: 'platform', credentialsId: 'sonar-payments') {
  sh 'sonar-scanner'
}
```

A following `waitForQualityGate` works without polling, because the platform
registers the SonarQube webhook that calls Jenkins back.

## Agents

An administrator creates the node, because Jenkins checks agent creation
against the controller rather than against a node that does not exist yet.
Name it with the team prefix so the node role matches, make it an inbound
agent over WebSocket, and hand the team its secret. The controller has zero
executors and its inbound TCP port is disabled, so every build runs on a team
agent that dials out through the HTTPS edge.

Labels are shared across teams: respect the prefix convention, or one team can
target another team's agent. `examples/agents/linux/` is a container agent you
can copy, and the [quickstart example](quickstart-example.md) creates one
end to end.
