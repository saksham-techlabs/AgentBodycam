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

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/) style messages,
for example `feat: add session event model` or `fix: handle missing git binary`.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
