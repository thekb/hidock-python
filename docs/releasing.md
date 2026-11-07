# Publishing hidock-python

Package name: `hidock-python`. Import package: `hidock`. CLI: `hidock`.
The first release is `0.1.0`. PyPI will not let you replace an uploaded version.

## One-time PyPI setup

Sign in to your PyPI account (with verified email and two-factor authentication),
then open https://pypi.org/manage/account/publishing/ and add a **pending publisher**:

| Field | Value |
| --- | --- |
| PyPI project name | `hidock-python` |
| Owner | `thekb` |
| Repository name | `hidock-python` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

This authorizes only this repository/workflow/environment to create and publish
the PyPI project. No PyPI API token is stored in GitHub. See PyPI's
[pending publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
If the project already exists under your account, add the publisher through
the project's Publishing settings instead.

## Build and validate locally

```sh
python -m pip install -e . build twine
python -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
```

Use a clean checkout for each release so `dist/` contains only the version to
publish. Inspect the wheel and source archive to confirm only source, tests,
documentation, metadata, and the license are included. Do not include recordings,
transcripts, local models, virtual environments, or credentials.

## Publish

1. Update the version in `pyproject.toml`, commit, and push to `main`.
2. Ensure the pending/trusted publisher above is configured on PyPI.
3. Create a GitHub release tagged `v0.1.0` (use the matching version for later
   releases). Publishing the release starts `.github/workflows/publish.yml`.
4. Watch the Actions run through tests, build, validation, and publishing.
5. Confirm https://pypi.org/project/hidock-python/ shows the expected release,
   then install it in a fresh environment with `pip install hidock-python`.

You can run the workflow manually with publishing disabled to validate artifacts
without uploading. For the initial release, you can also run it manually with
`publish=true` after configuring PyPI. Publishing the same version again fails;
do not create another publishing run for a version already uploaded.

Only the final publishing job receives the GitHub OIDC `id-token: write`
permission. Build and test jobs have read-only repository access.
