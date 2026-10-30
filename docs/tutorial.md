# Tutorial: from zero to a working connector

This walks through the whole setup. You end up with mailhub-mcp running in Docker behind a public HTTPS URL, added as a custom connector in claude.ai.

An instance belongs to one person: you link your own mailboxes (personal, work, a side project...) and only your Google login may use the connector. If someone else wants the same, they run their own instance; see [More than one person](#14-more-than-one-person).

## 1. Prerequisites

- A Linux host (or any machine) with Docker and Docker Compose, reachable from the internet over HTTPS (see step 7).
- A workstation with a browser, [uv](https://docs.astral.sh/uv/) and a clone of this repository. Linking mailboxes opens a browser, so do it there.
- Access to each of your mailboxes you want to connect (one Google sign-in per mailbox).

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
uv run --extra authorize mailhub-authorize gmail client_secret_desktop.json alice-work
uv run --extra authorize mailhub-authorize gmail client_secret_desktop.json alice-side-project
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
| `ALLOWED_EMAILS` | Your own Google login, the one you use to sign in to the connector. Every allowed login sees every linked mailbox, so list only yourself; the server logs a warning at startup if there is more than one address (comma-separated). Other people run their own instance |
| `HOST_PORT` | Optional. Port on `127.0.0.1` of the host the container is published on, default `8742`. Change it for a second instance on the same host |
| `CONTAINER_NAME`, `COMPOSE_PROJECT_NAME` | Optional. Container name (default `mailhub-mcp`) and Compose project name (default: the directory name). Give each instance on a host its own |
| `PORT` | Optional. Port the server listens on inside the container, default `8742`. The compose file maps it automatically; normally leave it alone |
| `MAX_CHANGES_PER_DAY` | Optional. Most messages `modify_emails` and `modify_by_query` may change per account in a rolling 24 hours, default `2000`. Undo is never capped |
| `TOOLS` | Optional. `all` (default) or `read-only`; see "Scheduled triage" below |
| `JWT_SIGNING_KEY` | Signs issued tokens and encrypts the OAuth state. Generate with `openssl rand -hex 32` |

`ACCOUNTS_DIR` and `JOURNAL_PATH` are set by the Dockerfile (`/secrets/accounts`, `/data/changes.jsonl`) and normally left alone. Preferences are stored next to the journal, in `data/preferences.json`. Changing `JWT_SIGNING_KEY` later invalidates sessions and you must reconnect the connector.

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

The compose file publishes the server on `127.0.0.1:8742` only (`HOST_PORT`), with a read-only filesystem, all capabilities dropped and a memory limit. On startup it logs a warning if no accounts are found, and another if `ALLOWED_EMAILS` lists more than one address.

## 7. Expose it over public HTTPS

claude.ai connects from the internet, so the server needs:

- HTTPS at the root of a host name (the OAuth discovery endpoints live under `/.well-known/`, and they must be reachable there);
- `PUBLIC_BASE_URL` set to exactly that URL, and the same value used in the Google redirect URI.

Options: [examples/tailscale-funnel/](../examples/tailscale-funnel/) (a complete example, including a firewall for the container), or any reverse proxy or tunnel with TLS that forwards to `127.0.0.1:8742` (or your `HOST_PORT`).

## 8. Add the connector in claude.ai

Settings, Connectors, **Add custom connector**, with the URL `<PUBLIC_BASE_URL>/mcp`. You are shown the server's consent screen, then the Google login. Sign in with your own Google login, the one in `ALLOWED_EMAILS`.

## 9. Verify

From a network outside your own (a phone on mobile data, say), this should return JSON:

```
<PUBLIC_BASE_URL>/.well-known/oauth-authorization-server
```

Then in a claude.ai chat with the connector enabled, ask Claude to call `list_accounts`. Each account should show `status: ok`.

Optionally evaluate FastMCP host/origin protection for your reverse proxy. It is not enabled by default; every endpoint except `/health` requires the Google OAuth login either way.

## 10. Operate

- **Audit**: `docker compose logs mailhub-mcp | grep tool=` shows every tool call with its arguments. Rejected logins appear as `rejected token for email=...`.
- **Add an account**: link it on your workstation (step 3; the folder on the server is owned by uid 10001, so never run `mailhub-authorize` inside it), copy `secrets/accounts/gmail/<alias>.json` to the server as in step 5, then on the server run `sudo chown 10001:10001` and `sudo chmod 600` on the file and `docker compose restart`. The same flow re-links an expired account. Accounts are read at startup.
- **Remove an account**: delete `secrets/accounts/<provider>/<alias>.json`, run `docker compose restart`, and revoke the app at https://myaccount.google.com/permissions. Without the account's credential, its past changes can no longer be undone.
- **Journal**: `data/changes.jsonl`, one JSON object per change (label changes and preference changes).
- **Preferences**: `data/preferences.json`. Proposals that lower attention are approved with `mailhub-admin`, see step 11.
- **Revoke everything**: `docker compose down`, remove the public exposure, and revoke the app in your Google accounts.

### Backup and restore

- Back up `data/` (journal, preferences, OAuth state), `secrets/accounts/` and `.env`. Stop the container first (`docker compose stop`) or copy the files atomically, so a write is not caught half done. The secrets are credentials: keep the backup private and encrypted.
- To restore, put the files back (with the same `.env`: without the same `JWT_SIGNING_KEY` the OAuth state cannot be decrypted and you must reconnect the connector), then fix ownership and permissions:

```bash
sudo chown -R 10001:10001 data secrets/accounts
sudo find secrets/accounts data -type d -exec chmod 700 {} +
sudo find secrets/accounts data -type f -exec chmod 600 {} +
docker compose up -d
```

### Updating

`git pull`, then `docker compose up -d --build`, then check `docker compose logs mailhub-mcp`. Reconnecting the connector in claude.ai is not needed unless `JWT_SIGNING_KEY` changed.

### Journal growth

`data/changes.jsonl` is append-only and each change adds a small line, so it grows slowly. To archive it: `docker compose stop`, move `data/changes.jsonl` aside (for example to `data/changes-2026.jsonl.bak`), `docker compose up -d`. Archived changes can no longer be undone or listed.

## 11. Preferences and approving proposals

`get_preferences` and `set_preferences` let Claude remember which senders and keywords matter (`important_senders`, `ignored_senders`, `important_keywords`, `ignored_keywords`) and when triage last ran (`last_triage_at`). Claude should change them only when you ask in the conversation, and every change records your words as `user_request`.

The direction rule decides what applies:

- Raising attention (marking a sender or keyword important, removing something from an ignored list) applies immediately. `undo_change` on it lowers attention again, so the undo becomes a proposal you approve like any other. Once approved, the change counts as undone; if you reject it, Claude cannot propose that undo again (make the change with a new request instead). Keywords may contain only letters, digits, spaces, apostrophes, dots and hyphens.
- Lowering attention (ignoring a sender or keyword, removing something from an important list) never applies from the chat. It becomes a proposal with an id such as `p-1a2b3c4d`, valid for 14 days, and at most 50 can be open at once. As long as you only approve proposals you recognize, a hostile email cannot quietly hide future alerts. Raising-attention changes apply without review, so check `list_changes` now and then; they can be undone.

Approve or reject proposals from a terminal on the server, outside the chat:

```bash
docker compose exec mailhub-mcp mailhub-admin preferences list
docker compose exec mailhub-mcp mailhub-admin preferences show <id>
docker compose exec mailhub-mcp mailhub-admin preferences approve <id>
docker compose exec mailhub-mcp mailhub-admin preferences reject <id>
```

`show` prints the scope, field, values and the request that motivated it, so check that it matches something you actually asked for before approving. Approving applies it and records a change (`approve_preference`) that appears in `list_changes`; rejecting removes the proposal. The command exits with 0 on success and 2 on an error such as an unknown or expired id.

### Prompts

The server also offers three MCP prompts, which Claude clients list as ready-made workflows: `daily_triage` (optionally with `accounts`, a comma-separated list of aliases), `clean_newsletters` and `organize_inbox` (both take an `account`). The last two preview every change and wait for your explicit confirmation in the conversation; nothing is trashed or deleted, and every change can be undone with `undo_change` or `undo_since`. With `TOOLS=read-only` only a read-only `daily_triage` is offered. Messages also show `unsubscribe` options when the sender provides them; Claude shows these to you and never visits them.

### Scheduled triage

Preferences make a recurring routine (a scheduled Claude task, or a prompt you run each morning) consistent. A recommended flow:

1. Call `get_preferences`: note the rules and `last_triage_at`.
2. Call `search_emails` for mail since `last_triage_at` (for Gmail, `after:YYYY/MM/DD`; fall back to a recent window when it is unset).
3. Read the results in bulk with `read_emails`.
4. Report, grouped as: **needs attention** (important senders and keywords first), **FYI**, **suspicious emails** (those with `suspicious: true` and their reasons; Claude should only report them, never act on what they say) and **preference changes** since the last run (`get_preferences` → `recent_changes`) and **pending proposals** (`get_preferences` → `pending`), with the exact `mailhub-admin preferences approve <id>` command for each pending proposal.
5. Call `set_preferences` with `scope: "global"`, `last_triage_at` set to now, and a `user_request` that describes the routine (for example "scheduled morning triage, as configured by me").

`last_triage_at` is plain data, so recording it needs a write tool. For routines that must only report, run the server with `TOOLS=read-only`: only the read tools are registered, nothing can be changed, and step 5 is impossible. The trade-off is that `last_triage_at` is never updated, so each run either re-reports mail it already covered (use a fixed window such as `newer_than:1d`) or you run a second, full instance of your own (its own directory, `.env`, `data/` folder, `HOST_PORT`, `CONTAINER_NAME` and `COMPOSE_PROJECT_NAME`, as in [More than one person](#14-more-than-one-person); copy `data/preferences.json` if you want to share rules) just for the routines that record the timestamp. A read-only instance still lets Claude read preferences and proposals.

Drafts are replies only. `create_draft` needs the message being answered, accepts only recipients who are already in that thread (plus your own address), and is refused for a thread containing an email flagged as suspicious; nothing is ever sent.

### Health checks

`GET /health` answers `{"status":"ok"}` without authentication and with nothing else; the Docker image uses it for its `HEALTHCHECK`, so `docker compose ps` shows `healthy` or `unhealthy`. That only means the server is up. To find out whether the linked mailboxes still work (for example an expired credential), run:

```bash
docker compose exec mailhub-mcp mailhub-admin accounts
```

It prints one line per account, `alias  provider  ok  <address>` or `alias  provider  ERROR  <message>`, and exits 0 when all are ok, 1 when any failed and 2 when the accounts cannot be loaded. Run it from cron or a scheduled task and notify yourself on a non-zero exit.

## 12. Recovery

If something went wrong and you need to revert in bulk:

- Ask Claude to call `list_changes` to see recent changes (newest first, with their `change_id`s, also from earlier sessions), then `undo_change` with the `change_id` of the one to revert.
- To revert everything since a time, call `undo_since` with `since` (ISO 8601, e.g. `2026-11-01T08:00`; no timezone means UTC). It previews by default: review the list, then call it again with `confirm=true`. It undoes newest first, at most 50 undone per call; repeat while `remaining` is true. Changes that could not be undone, and older ones held back because they touch the same emails, are listed in `failed`: fix the cause (for example a temporary Gmail error) and call again. Do not repeat the call when nothing was undone.
- Preference changes are in the same history: `list_changes` shows them with `kind: "preferences"`, and `undo_change` or `undo_since` restores the earlier values. Undo is refused if the preference changed again since, so a later change is never overwritten.
- In Gmail, search `label:claude/organized`: every message changed through the server has it. (`undo_change` removes the label together with the rest of that change, so undone messages no longer show up there.)
- Fallback when `list_changes` is unavailable (for example the connector is not responding): read the IDs straight from `data/changes.jsonl` on the host, newest first, and call `undo_change` for each once the server is back. List them in that order with:

```bash
jq -r '.change_id' data/changes.jsonl | tac
```

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `redirect_uri_mismatch` from Google | The Web client's redirect URI differs from `<PUBLIC_BASE_URL>/auth/callback` |
| Connector fails to connect, or every call returns 401 | The Google account is not in `ALLOWED_EMAILS` (look for `rejected token` in the logs) |
| `invalid_redirect_uri` when connecting | Claude's callback URL changed; update `CLAUDE_REDIRECT_URIS` in `src/mailhub_mcp/auth.py` |
| `invalid_grant` when reading mail after several days | The app is in *Testing*: publish it (step 2.3) and run `mailhub-authorize` again for that alias |
| `list_accounts` is empty, log warning mentions `/secrets/accounts` | Folder missing or no `<provider>/<alias>.json`; invalid alias in a file name; unknown provider folder |
| Startup fails with a duplicate alias error | The same alias exists under two provider folders; rename one file |
| Container restarts with `PermissionError` | `data/` or `secrets/accounts/` is not owned by uid 10001 |
| `would exceed the daily change cap` | The request plus the messages already changed on that account in the last 24 hours would go over `MAX_CHANGES_PER_DAY`; raise it in `.env` and restart, or wait. Undo still works |
| `create_draft` says drafts are replies only, or lists allowed addresses | The draft needs `reply_to_message_id`, and `to` and `cc` must be people already in that thread |
| `create_draft` says the thread is flagged as a possible prompt injection | A message in the thread matched the injection heuristics; reply manually in Gmail |
| A write tool (`modify_emails`, `set_preferences`, ...) does not exist | The server runs with `TOOLS=read-only`; set `TOOLS=all` and restart |
| Startup fails with `TOOLS must be 'all' or 'read-only'` | Invalid `TOOLS` value in `.env` |
| `set_preferences` returns `pending` instead of applying | The change lowers attention; approve it with `mailhub-admin preferences approve <id>` |
| Log warning `ALLOWED_EMAILS has N addresses` | More than one login can use this instance, and each sees every linked mailbox. Keep only the owner's login and give other people their own instance (step 14) |
| Second instance fails with a container name or port already in use | It shares `CONTAINER_NAME`, `HOST_PORT` or `COMPOSE_PROJECT_NAME` with the first one (step 14) |
| `docker compose ps` shows `unhealthy` | The container is not serving: check `docker compose logs` for startup errors (missing environment variables in `.env`, permissions on `data/` or `secrets/accounts/`) |
| `mailhub-admin accounts` prints `ERROR` for an alias | The credential no longer works; run `mailhub-authorize` again for that alias (see `invalid_grant` above) |
| `Too many pending proposals` | 50 proposals are open; review them with `mailhub-admin preferences list` |
| `unknown or expired proposal` from `mailhub-admin` | Proposals expire after 14 days; ask Claude to propose it again |
| `undo_change` says a preference "changed since" | The field was modified after that change; undo the later change first |
| "temporary failure, retry" or `rateLimitExceeded` in the logs | Gmail's per-user rate limit. Calls are paced (about 25 messages per second) and retried after 5, 15 and 30 seconds, so large cleanups take a while; if it still fails, wait a minute and retry. Avoid running several large `inbox_overview` calls back to back |

## 14. More than one person

mailhub-mcp is designed for one owner per instance: every login in `ALLOWED_EMAILS` sees every linked mailbox, and preferences, history and undo are shared by everything in the instance. If another person wants the same, run a separate instance for them. They can live on the same host:

1. A separate directory with its own copy of the project, for example `/opt/mailhub-mcp-2` next to `/opt/mailhub-mcp`.
2. Its own `.env`: their `ALLOWED_EMAILS` (only their login), their own `JWT_SIGNING_KEY`, and a `PUBLIC_BASE_URL` of their own (a different host name, or the same host on another HTTPS port, as in the Tailscale example). Give it a different `HOST_PORT` (for example `8743`), `CONTAINER_NAME` (for example `mailhub-mcp-2`) and `COMPOSE_PROJECT_NAME` (for example `mailhub-mcp-2`), so the two containers, ports and Compose projects do not collide.
3. Their own linked accounts in that directory's `secrets/accounts/` (step 3, signed in with their mailboxes) and their own `data/` (journal, preferences, OAuth state). Never copy one person's `secrets/` or `data/` into another instance.
4. Their own public HTTPS URL forwarding to `127.0.0.1:<their HOST_PORT>`, and `<their PUBLIC_BASE_URL>/auth/callback` added as an authorized redirect URI of the Google Web client (the same Google Cloud project and clients can serve both instances; just add the second redirect URI).
5. They add their own custom connector in claude.ai with `<their PUBLIC_BASE_URL>/mcp` and log in with their own Google account.

Run each instance's `docker compose` and `mailhub-admin` commands from its own directory. If the instances get the example firewall, give each its own network and subnet; see [examples/tailscale-funnel/](../examples/tailscale-funnel/#more-than-one-person).
