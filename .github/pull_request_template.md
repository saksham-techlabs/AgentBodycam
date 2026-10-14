**What and why**

**Checklist**

- [ ] `python scripts/check.py` passes locally
- [ ] If `web/` changed: `npm run typecheck`, `npm run test` and `npm run build` pass, and the
      regenerated `src/agentbodycam/ui/static/` is committed
- [ ] Docs and `CHANGELOG.md` updated if behaviour or claims changed
- [ ] No secrets, tokens, personal paths or AgentBodycam session data (`.agentbodycam/`,
      exports of private projects) in the diff, screenshots or description

CI runs the full matrix (Windows, Linux, macOS; Python 3.11-3.14) on this pull request;
you do not need to test every platform locally.
