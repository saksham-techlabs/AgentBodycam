# Contributing to AgentBodycam

Thanks for your interest. The project is in early development, so the architecture is
still settling. Please open an issue to discuss significant changes before starting work.

## Ground rules

1. **Local-first and privacy-first.** No telemetry, no required network calls, no accounts.
2. **No required AI.** Core features must work without any external AI API.
3. **Facts vs. inferences.** Code that produces findings must label them as `observed` or
   `inferred` and link them to evidence. Never present an inference as fact.
   See [docs/architecture.md](docs/architecture.md#2-observed-fact-vs-inference).
4. **Honest claims.** Do not document an agent integration or feature as supported until
   it is implemented and tested.
5. **Minimal dependencies.** Prefer the standard library. Justify any new runtime dependency.
6. **Cross-platform.** Code must work on Windows, Linux and macOS. Use `pathlib`.

## Development setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Checks

All of these must pass before a change is merged:

```bash
python scripts/check.py
```

This runs `ruff format --check`, `ruff check`, `mypy` (strict, once each for Windows, Linux
and macOS via `--platform`, so platform-specific branches are type-checked from any OS)
and `pytest`.
Use `python scripts/check.py --fix` to auto-format first.

`tests/test_packaging.py` (part of the normal test run) builds the wheel and sdist with
hatchling, inspects both, checks that `agentbodycam.__version__`, `agentbodycam --version`,
the wheel metadata and `web/package.json` agree, and installs the wheel offline into a
fresh virtual environment to run the CLI and the Replay UI from it with only that
environment on `PATH` (no Node.js, Git or source checkout). To run just these tests:

```bash
python -m pytest tests/test_packaging.py
```

To check artifacts you built yourself, point the same tests at them (the directory must
hold exactly one wheel and one sdist of the current version):

```bash
python -m hatchling build                                   # writes dist/
AGENTBODYCAM_TEST_DIST=dist python -m pytest tests/test_packaging.py
# PowerShell: $env:AGENTBODYCAM_TEST_DIST = "dist"; python -m pytest tests/test_packaging.py
```

Release artifacts in `dist/` are never committed. The sdist is built from an explicit
allowlist in `pyproject.toml`; if you add a new top-level file or `web/` config file that
downstream builders need, add it there. (`.github/` is deliberately not in the sdist.)

## Replay UI (`web/`)

The Replay UI is a React + TypeScript + Vite app. Node.js 22 (22.13 or newer) or 24 is
needed only to work on it (see `engines` in `web/package.json`); users of AgentBodycam never
need Node because the production build is committed to `src/agentbodycam/ui/static/` and
shipped in the Python package.

```bash
cd web
npm ci
npm run typecheck
npm run test
npm run build      # rewrites src/agentbodycam/ui/static/
```

After any change in `web/`, run `npm run build` and commit the regenerated
`src/agentbodycam/ui/static/` together with the source change. CI rebuilds the UI and fails
if the result differs from the committed files in any way. Keep the UI compatible with
its Content Security Policy: no inline scripts or styles in `index.html`, no `eval`, no
remote fonts, CDNs or analytics, and never store the API token anywhere but in memory.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

- **Frontend** (Ubuntu, Node 22 and 24): `npm ci`, `npm run typecheck`, `npm run test`,
  `npm run build`, then fails if the build differs from the committed
  `src/agentbodycam/ui/static/`.
- **Python** (Windows, Linux and macOS, each with Python 3.11, 3.12, 3.13 and 3.14):
  `python scripts/check.py`, including the platform-specific, security/privacy,
  no-network and packaging tests.
- **Package** (Ubuntu, after both of the above pass): builds the wheel and sdist, runs
  `tests/test_packaging.py` against exactly those files and keeps them as a workflow
  artifact for 7 days.

You only need to run the checks on your own platform; CI covers the other operating
systems and Python versions after you open a pull request. Platform-specific tests skip
themselves with a stated reason (shown in the pytest summary) where a feature does not
exist, for example POSIX signals on Windows or junctions on Linux and macOS.

`.github/workflows/release-build.yml` is started by hand. It runs the same CI and keeps
the verified wheel and sdist for 30 days. It publishes nothing; releasing is a manual
process described in [docs/release-checklist.md](docs/release-checklist.md).

No workflow uses repository secrets, and all of them only have read access to the
repository.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/) style messages,
for example `feat: add session event model` or `fix: handle missing git binary`.
Keep one logical change per commit, never commit `dist/`, `node_modules/`, `.agentbodycam/`
or other local data (a test checks the tracked files), and do not rewrite history that
has already been pushed.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
