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

This runs `ruff format --check`, `ruff check`, `mypy` (strict) and `pytest`.
Use `python scripts/check.py --fix` to auto-format first.

`tests/test_packaging.py` builds the wheel and sdist with hatchling, inspects both, and
installs the wheel offline into a fresh virtual environment to run the CLI and the Replay
UI from it with only that environment on `PATH` (no Node.js, Git or source checkout).
Release artifacts (`python -m hatchling build` writes them to `dist/`) are never
committed. The sdist is built from an explicit allowlist in `pyproject.toml`; if you add
a new top-level file or `web/` config file that downstream builders need, add it there.

## Replay UI (`web/`)

The Replay UI is a React + TypeScript + Vite app. Node.js 20+ is needed only to work on
it; users of AgentBodycam never need Node because the production build is committed to
`src/agentbodycam/ui/static/` and shipped in the Python package.

```bash
cd web
npm ci
npm run typecheck
npm run test
npm run build      # rewrites src/agentbodycam/ui/static/
```

After any change in `web/`, run `npm run build` and commit the regenerated
`src/agentbodycam/ui/static/` together with the source change. Keep the UI compatible with
its Content Security Policy: no inline scripts or styles in `index.html`, no `eval`, no
remote fonts, CDNs or analytics, and never store the API token anywhere but in memory.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/) style messages,
for example `feat: add session event model` or `fix: handle missing git binary`.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
