# Unattended agent dispatcher

Runs the multi-agent workflow headless on an always-on server (homelab) so
the project progresses without anyone launching sessions manually.

## One-time setup (on the homelab)

1. Install the CLI and authenticate:
   ```bash
   uv tool install mistral-vibe
   # put your key in ~/.vibe/.env :  MISTRAL_API_KEY=...
   ```
2. Clone this repo with push rights:
   ```bash
   gh auth login          # or use a deploy key
   git clone https://github.com/pennico/immich-geotag.git ~/projects/immich-geotag
   ```
3. Smoke-test the state machine without launching anything:
   ```bash
   cd ~/projects/immich-geotag && python3 dispatcher/dispatch.py --dry
   ```
4. Schedule it (every 3 hours; overlapping runs are skipped by flock):
   ```bash
   crontab -e
   # add:
   0 1-23/3 * * * ~/projects/immich-geotag/dispatcher/run.sh >> ~/projects/immich-geotag/dispatcher/logs/cron.log 2>&1
   ```
   (offset from the tts-ereader cron so the two projects don't run sessions
   at the same time)

## What a tick does

1. Abort if the working tree is dirty; checkout main; pull --ff-only.
2. Open PRs? -> any without the `approved` label triggers a QA session;
   all approved + green checks -> squash-merge.
3. No PRs -> Implementation session on the first unchecked ROADMAP.md item.
4. Roadmap complete -> idle.

## Guardrails

- `.vibe/hooks.toml` + `.vibe/guard.py` deny destructive bash commands
  (force-push, reset --hard, rm -rf, sudo, pipe-to-shell) and any agent
  access to `.env`. Loaded because the dispatcher passes `--trust`.
- Sessions run with `--auto-approve --max-turns 150` and a 90-minute
  timeout; output is captured, never interactive.
- The dispatcher never force-pushes, never merges unapproved PRs, and
  aborts on a dirty tree.
- Extra safety for this project: agents are instructed never to write to
  the Immich server; live validation is the owner's job (see AGENTS.md).

## Logs

`dispatcher/logs/` (gitignored): one JSON file per session, an `.exit`
file with its return code, and `cron.log`.
