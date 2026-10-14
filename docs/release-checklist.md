# Release checklist

AgentBodycam has not been released or published anywhere. This is the manual process for
the first (and later) releases. No workflow publishes, tags or releases automatically;
every step that leaves the machine is done by hand, and only after the previous steps
pass.

## Before publishing anything

1. **Clean Git tree.** `git status` shows nothing to commit; no `dist/`, `node_modules/`,
   `.agentbodycam/` or other local files are tracked.
2. **Local checks green.** `python scripts/check.py`; in `web/`: `npm ci`,
   `npm run typecheck`, `npm run test`, `npm run build`, then `git status` still clean
   (the committed UI assets match the build).
3. **Independent audit resolved.** Every finding of the independent review is fixed or
   explicitly accepted and documented.
4. **Version reviewed.** `agentbodycam.__version__` (`src/agentbodycam/__init__.py`),
   `web/package.json` and `web/package-lock.json` agree (a test enforces this). Bump them
   together if needed.
5. **CHANGELOG reviewed.** Move `[Unreleased]` entries under the new version and date.
6. **README reviewed.** Status table, install instructions and supported-platform claims
   match what has actually been verified.
7. **Security and privacy checks green.** Part of step 2; also re-read
   [SECURITY.md](../SECURITY.md) for stale statements.
8. **Wheel and sdist built.** `python -m hatchling build` writes `dist/`.
9. **Installed-wheel smoke.**
   `AGENTBODYCAM_TEST_DIST=dist python -m pytest tests/test_packaging.py` inspects
   exactly those files and runs them from a fresh virtual environment without Node.js.

## Publishing the repository

10. **Create the GitHub repository** (empty, no generated README or license).
11. **Add the real project URLs and badges.** `[project.urls]` in `pyproject.toml`
    (Repository, Issues, Homepage) and CI badges in the README, using the real URL only.
    Add the private vulnerability-reporting channel to SECURITY.md.
12. **Push** `main`.
13. **Confirm the first hosted CI run is green** on every OS and Python version (CI has
    never run before this point). Fix anything platform-specific, then update the
    README's "verified so far" statement to what CI actually covered.

## Releasing

14. **Tag and release only after CI is green** on the exact commit. Run the
    **Release build** workflow on it and use its `agentbodycam-dist-<sha>` artifact.
15. **Verify the downloadable artifacts.** Download the release files, check their
    SHA-256 against the workflow artifact, install the wheel into a fresh environment and
    run `agentbodycam --version`, `agentbodycam doctor` and `agentbodycam replay`.

Publishing to PyPI or any other index is a separate, later decision and is not part of
this checklist.
