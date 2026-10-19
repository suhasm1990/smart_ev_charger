# Project-Scoped Rules

## Git & Pull Request Guidelines
- When dispatched to create or update a Pull Request, you ARE authorized to create a dedicated feature branch, commit changes, push to origin, and open a PR with `gh pr create`.
- Never commit directly to `main` — always create a new branch (e.g. `fix/...` or `docs/...`).

## Deployment: Changes Must Work With a Restart Alone
- Every change must take effect with just "push to `main` + restart" (Telegram `/update` or a container restart). On each start, `entrypoint.sh` runs `git pull` and `pip install -r requirements.txt`, then launches `main.py`.
- Do NOT require a new Docker image or redeploy except in extreme, unavoidable cases. If one truly is unavoidable, say so explicitly up front, explain why, and list the exact rebuild steps.
- `.env` and `service_account.json` are baked into the image at build time and copied over `/app` on every start, so new or changed `.env` values never reach the server through a push. Instead:
  - give every new setting a sensible default in `core/config.py` (env var stays optional, as an override), or
  - make it runtime-tunable via `DYNAMIC_CONFIG_SCHEMA` (persisted in Google Sheets, changeable from Telegram).
- Changes that need a rebuild, and so must be avoided: editing `Dockerfile` or `entrypoint.sh`, new system (apt) packages, new required secrets or credential files, and anything that depends on a new `.env` value having no code default.
- New pure-Python dependencies in `requirements.txt` are fine (installed on start), but keep them minimal: a failed install there does not stop startup.
