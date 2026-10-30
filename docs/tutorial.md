# Tutorial: from zero to a working connector

This walks through the whole setup. You end up with mailhub-mcp running in Docker behind a public HTTPS URL, added as a custom connector in claude.ai.

## 1. Prerequisites

- A Linux host (or any machine) with Docker and Docker Compose, reachable from the internet over HTTPS (see step 7).
- A workstation with a browser, [uv](https://docs.astral.sh/uv/) and a clone of this repository. Linking mailboxes opens a browser, so do it there.
- A Google account for each mailbox you want to connect.

## 2. Google Cloud

Do this once. First decide the public HTTPS URL the server will be reachable at (`PUBLIC_BASE_URL`, e.g. `https://mail.example.com`; see step 7 and [examples/](../examples/)), because the Google Web client needs `<PUBLIC_BASE_URL>/auth/callback` as its redirect URI.

1. Create a project at https://console.cloud.google.com and enable the **Gmail API**.
2. In **Google Auth Platform**, set up Branding and Audience: user type **External**, fill in an app name and contact email.
3. Under **Audience**, click **Publish app** so the status is *In production*. While the app is in *Testing*, Gmail refresh tokens expire after 7 days. For personal use, an unverified production app works; Google only shows an "unverified app" warning at login (Advanced, then Continue). That warning is expected.
4. Under **Clients**, create two OAuth clients:
   - **Web application**, used for the connector login. Authorized redirect URI: `<PUBLIC_BASE_URL>/auth/callback` (for example `https://mail.example.com/auth/callback`). Note its client ID and secret for `.env`.
   - **Desktop app**, used to link mailboxes. Download its JSON, for example as `client_secret_desktop.json`.

## 3. Link your mail accounts

Run this once per mailbox, with one alias per mailbox:

```bash
uv run --extra authorize mailhub-authorize gmail client_secret_desktop.json alice-personal
uv run --extra authorize mailhub-authorize gmail client_secret_desktop.json bob-work
```

- The first argument is the provider (`gmail`).
- The alias must match `^[a-z0-9][a-z0-9-]{0,31}$`: lowercase letters, digits and hyphens, up to 32 characters, starting with a letter or digit.
- Sign in with the matching Google account and accept the `gmail.modify` scope.
- The credential is written to `secrets/accounts/<provider>/<alias>.json` with mode 600. The file name is the alias used in the tools. An alias cannot exist under two providers; re-running for the same provider and alias relinks the account. `--accounts-dir` changes the output folder.
- Link on your workstation, where the browser is. Re-running the command later (re-linking, adding accounts) always happens there, never inside the folder the container user owns; step 5 and the "Add an account" operation copy the result to the server. If the server is the same machine as your workstation, link with `--accounts-dir ./staging` and copy the file into `secrets/accounts/gmail/` with `sudo cp`, then set owner and mode as in step 6.
- Google Workspace administrators can block unverified apps. If linking a work account fails with an access error, ask your admin to allow the app, or use a different account.

## 4. Configure

```bash
cp .env.example .env
```

| Variable | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | The exact public HTTPS URL, no trailing slash and no `/mcp`, e.g. `https://mail.example.com` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From the Web application client |
| `ALLOWED_EMAILS` | Comma-separated Google accounts allowed to log in to the connector |
| `PORT` | Optional. Port the server listens on inside the container, default `8742`. If you change it, change both numbers in the `ports` mapping in `docker-compose.yml` to match |
| `JWT_SIGNING_KEY` | Signs issued tokens and encrypts the OAuth state. Generate with `openssl rand -hex 32` |

`ACCOUNTS_DIR` and `JOURNAL_PATH` are set by the Dockerfile (`/secrets/accounts`, `/data/changes.jsonl`) and normally left alone. Changing `JWT_SIGNING_KEY` later invalidates sessions and you must reconnect the connector.

## 5. Put the code and credentials on the server

If the server is another machine, clone this repository there and copy your credentials, keeping the modes (directories 700, files 600):

```bash
rsync -av --chmod=D700,F600 secrets/accounts/ <server>:<project-dir>/secrets/accounts/
```

This works the first time, before step 6 hands the folder to uid 10001. Later copies (adding or re-linking an account) target a folder your user cannot write to, so let rsync run with sudo on the server (`--rsync-path="sudo rsync"`, which needs passwordless sudo there) or copy to a temporary folder and `sudo cp` the file into place.

Also create `.env` on the server (step 4). If everything runs on one machine, skip the copy.

## 6. Run with Docker

The container runs as uid 10001. It needs to own `data/` and read `secrets/accounts/`:

```bash
mkdir -p data
sudo chown -R 10001:10001 data secrets/accounts
sudo chmod 600 .env secrets/accounts/*/*.json
sudo chmod 700 data secrets/accounts secrets/accounts/*

docker compose up -d --build
docker compose logs -f
```

The compose file publishes the server on `127.0.0.1:8742` only, with a read-only filesystem, all capabilities dropped and a memory limit. On startup it logs a warning if no accounts are found.

## 7. Expose it over public HTTPS

claude.ai connects from the internet, so the server needs:

- HTTPS at the root of a host name (the OAuth discovery endpoints live under `/.well-known/`, and they must be reachable there);
- `PUBLIC_BASE_URL` set to exactly that URL, and the same value used in the Google redirect URI.

Options: [examples/tailscale-funnel/](../examples/tailscale-funnel/) (a complete example, including a firewall for the container), or any reverse proxy or tunnel with TLS that forwards to `127.0.0.1:8742`.

## 8. Add the connector in claude.ai

Settings, Connectors, **Add custom connector**, with the URL `<PUBLIC_BASE_URL>/mcp`. You are shown the server's consent screen, then the Google login. Sign in with an account listed in `ALLOWED_EMAILS`.

## 9. Verify

From a network outside your own (a phone on mobile data, say), this should return JSON:

```
<PUBLIC_BASE_URL>/.well-known/oauth-authorization-server
```

Then in a claude.ai chat with the connector enabled, ask Claude to call `list_accounts`. Each account should show `status: ok`.

## 10. Operate

- **Audit**: `docker compose logs mailhub-mcp | grep tool=` shows every tool call with its arguments. Rejected logins appear as `rejected token for email=...`.
- **Add an account**: link it on your workstation (step 3; the folder on the server is owned by uid 10001, so never run `mailhub-authorize` inside it), copy `secrets/accounts/gmail/<alias>.json` to the server as in step 5, then on the server run `sudo chown 10001:10001` and `sudo chmod 600` on the file and `docker compose restart`. The same flow re-links an expired account. Accounts are read at startup.
- **Remove an account**: delete `secrets/accounts/<provider>/<alias>.json`, run `docker compose restart`, and revoke the app at https://myaccount.google.com/permissions. Without the account's credential, its past changes can no longer be undone.
- **Journal**: `data/changes.jsonl`, one JSON object per change.
- **Revoke everything**: `docker compose down`, remove the public exposure, and revoke the app in your Google accounts.

## 11. Recovery

If something went wrong and you need to revert in bulk:

- Ask Claude to call `list_changes` to see recent changes (newest first, with their `change_id`s, also from earlier sessions), then `undo_change` with the `change_id` of the one to revert.
- To revert everything since a time, call `undo_since` with `since` (ISO 8601, e.g. `2026-11-01T08:00`; no timezone means UTC). It previews by default: review the list, then call it again with `confirm=true`. It undoes newest first, at most 50 changes per call; repeat while `remaining` is true. Changes that could not be undone are listed in `failed`; retry them later.
- In Gmail, search `label:claude/organized`: every message changed through the server has it. (`undo_change` removes the label together with the rest of that change, so undone messages no longer show up there.)
- Fallback when `list_changes` is unavailable (for example the connector is not responding): read the IDs straight from `data/changes.jsonl` on the host, newest first, and call `undo_change` for each once the server is back. List them in that order with:

```bash
jq -r '.change_id' data/changes.jsonl | tac
```

## 12. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `redirect_uri_mismatch` from Google | The Web client's redirect URI differs from `<PUBLIC_BASE_URL>/auth/callback` |
| Connector fails to connect, or every call returns 401 | The Google account is not in `ALLOWED_EMAILS` (look for `rejected token` in the logs) |
| `invalid_redirect_uri` when connecting | Claude's callback URL changed; update `CLAUDE_REDIRECT_URIS` in `src/mailhub_mcp/auth.py` |
| `invalid_grant` when reading mail after several days | The app is in *Testing*: publish it (step 2.3) and run `mailhub-authorize` again for that alias |
| `list_accounts` is empty, log warning mentions `/secrets/accounts` | Folder missing or no `<provider>/<alias>.json`; invalid alias in a file name; unknown provider folder |
| Startup fails with a duplicate alias error | The same alias exists under two provider folders; rename one file |
| Container restarts with `PermissionError` | `data/` or `secrets/accounts/` is not owned by uid 10001 |
