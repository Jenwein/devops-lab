# Contributing

## Running the checks

```bash
python3 -m unittest discover -s tests -p 'test*.py'
python3 -m compileall -q scripts examples tests
```

The suite needs Docker for the Compose render tests and takes about twenty
seconds. Run it once more with both `DEVOPS_LAB_ROOT=/nonexistent/ambient` and
`COMPOSE_PROJECT_NAME=ambient` exported: the platform must ignore ambient
values in favour of its env files, and CI runs both variants.

## Keeping a fork mergeable

Put everything site-specific outside the tracked files: `runtime.env` in the
runtime root (identity, ports, memory limits), `config/jenkins/casc.d/` and
`config/sonarqube/plugins/` in the runtime root (Jenkins and SonarQube
customisation), and `secrets/`. In the repository, change only `versions.env`
to bump images and `config/jenkins/plugins.txt` to change the plugin set.
Anything else you change in tracked files will need a merge on every upstream
update.

## Pull requests

Keep changes small, add or adjust a test in `tests/`, and make sure the
`check` workflow is green. Documentation commands are verified by
`tests/test_docs.py`, so update the docs in the same pull request as a
command-line change.
