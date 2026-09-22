# Quickstart example

`examples/quickstart/run.sh` proves a running platform end to end. It creates
team `demo` the same way `scripts/lab add-team demo` does, registers an inbound
Jenkins node `demo-linux`, builds and starts the Linux agent in
`examples/agents/linux/`, creates GitLab project `demo/hello` and SonarQube
project `demo-hello`, pushes the sample under `hello/`, waits for the
organization folder to discover and build it, and checks the GitLab commit
status and the SonarQube quality gate. Evidence lands in
`${DEVOPS_LAB_ROOT}/evidence/quickstart.json`.

The agent joins the platform's `edge` network, so it resolves the Jenkins
name without host DNS, trusts only the platform CA for its controller
connection, and connects over WebSocket through the HTTPS edge. It runs in
its own Compose project, `<platform project>-quickstart`, so stopping it
never touches the platform. Stop it with:

```bash
examples/quickstart/run.sh --stop
```

Nothing here is required by `setup`, `up`, `status`, `backup` or `restore`.
The full walk-through, including the agent image and what the evidence file
proves, is in [quickstart example](../docs/quickstart-example.md).
