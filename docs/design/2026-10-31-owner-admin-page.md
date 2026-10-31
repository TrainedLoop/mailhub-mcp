# Owner admin page (`/admin`)

Status: proposed
Date: 2026-10-31

## Context

Linking a mailbox today needs a workstation with uv and a clone of the repository: `mailhub-authorize` runs Google's installed-app flow with a second OAuth client of type "Desktop", writes `secrets/accounts/<provider>/<alias>.json`, and the owner copies that file to the server, fixes its owner and mode, and restarts the container, because accounts are read once at startup. Re-linking an expired account is the same procedure. With a published Docker image, this is the main obstacle between "the container is running" and "my mailboxes are connected".

This design adds a small web page on the server, `/admin`, where the owner sees the linked mailboxes and links, re-links and removes them through the browser, with no restart and no command line.

### Goals

- Link a new Gmail mailbox, re-link an expired one and remove one from a browser, including a phone.
- Linked and removed accounts take effect immediately, without a restart.
- Re-linking an alias can only attach the same mailbox it had before, so the journal and undo of that alias never act on a different mailbox.
- Removing an account revokes its token at the provider.
- No MCP tool can reach any of these actions.
- No new required configuration and no database.

### Non-goals

- Approving or rejecting preference proposals. That is the next step and reuses the session and CSRF code from this one.
- Login providers other than Google. The identity check sits behind a small interface so Microsoft or generic OIDC (which covers self-hosted identity such as Pocket ID or Authelia) can be added later, for the MCP connector and `/admin` together.
- Removing `mailhub-authorize`. It stays as the command-line alternative, for example for operators who keep the credentials directory read-only.

### Threat model

`/admin` is served at the same public URL as the MCP endpoint, so anyone on the internet can reach its login page. Only a verified Google login listed in `ALLOWED_EMAILS` gets a session, the same rule as the connector. The design defends against:

- forged cross-site requests from pages the owner visits (CSRF);
- a stolen or forgotten session cookie (short session, recent re-authentication for every action);
- an OAuth callback replayed or injected by a third party (`state` bound to the session, PKCE);
- a re-link that attaches a different mailbox to an existing alias;
- a mailbox linked with the Gmail scope unticked on Google's granular consent screen;
- the MCP surface (and therefore injected email text) reaching credential writes.

Linking a mailbox still requires signing in to that mailbox's Google account and consenting there. A session on `/admin` alone cannot link someone else's mailbox.

## Design

### Components

- `mailhub_mcp/admin_web/` (new): routes, HTML rendering, session cookie, CSRF and re-authentication checks. Provider-agnostic.
- `mailhub_mcp/admin_web/identity.py`: the owner login. Exposes one operation, "the verified email of the person who just signed in", implemented with Google OIDC. Later login providers implement the same interface.
- `mailhub_mcp/providers/gmail/link.py` (new): the Gmail `Linker`. Builds the authorization URL and exchanges the code for a credential and the mailbox address. Core code only knows the `Linker` protocol (in `providers/base.py`), and the existing core/provider boundary test keeps it that way.
- `mailhub_mcp/core/accounts.py`: `AccountRegistry` gains `upsert(alias, provider_name, credential_path)` and `remove(alias)`, guarded by a lock. `all()`, `aliases()` and `get()` read a consistent snapshot. A tool call already holding an `Account` finishes with it.
- `mailhub_mcp/core/account_book.py` (new): `data/accounts.json`, mapping each alias to its provider, mailbox address, `linked_at` and, after removal, `removed_at`. Atomic writes. It survives removal so a later re-link of the alias can be checked against the address.

All routes are registered with `custom_route`, like `/health`, and only when `ADMIN` is on.

### Owner login and session

- **Login.** A separate OIDC authorization-code flow on the existing Google Web client (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`), not the connector's OAuth proxy. Redirect URI `<PUBLIC_BASE_URL>/admin/auth/callback`, scopes `openid email`, `prompt=select_account`, with `state`, `nonce` and PKCE. The callback verifies the ID token (signature against Google's keys, `aud`, `iss`, expiry, `nonce`) and requires `email_verified` and an email in `ALLOWED_EMAILS`.
- **Session cookie.** `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/admin`, signed with HMAC-SHA256 under a key derived from `JWT_SIGNING_KEY` (HKDF with a fixed `mailhub-admin-session` label, so the connector's key and the cookie key never coincide). It holds the email, the login time and the last authentication time, and expires 8 hours after login. No server-side session state. Every request re-checks the signature, the expiry and that the email is still in `ALLOWED_EMAILS`. Rotating `JWT_SIGNING_KEY` ends every session.
- **Recent re-authentication.** Link, re-link and remove run only if the last Google authentication is under 5 minutes old; otherwise the page sends the owner through Google and back to the same action. The login request asks for the `auth_time` claim with `claims={"id_token":{"auth_time":{"essential":true}}}`. When the ID token carries `auth_time`, that time is the last authentication. Google only sends it when the "session age claim" option is enabled in the Google Auth Platform console; without it, the time of the Google round trip counts instead, which only proves the browser has an active Google session. In that case `/admin` shows a notice recommending the option, and the tutorial explains how to enable it.
- **CSRF.** Every state-changing request is a POST carrying a token bound to the session (HMAC of the session id). The server also requires `Origin` (or, without it, `Referer`) to match `PUBLIC_BASE_URL`. `SameSite=Strict` is a third layer.
- **Logout** is a POST that clears the cookie.

### Linking a mailbox

1. `GET /admin/link` shows a form: alias (same rule as today, `^[a-z0-9][a-z0-9-]{0,31}$`, not `global`, not an alias that is linked or recorded as removed in `accounts.json`; a removed alias comes back only through re-link, so its journal never meets a different mailbox) and provider (Gmail only for now).
2. `POST /admin/link` checks CSRF, re-authentication and the alias, then redirects to Google with scopes `https://www.googleapis.com/auth/gmail.modify openid email`, `access_type=offline`, `prompt=consent select_account`, PKCE, and a signed `state` that binds the session, the action (`link` or `relink`), the alias and the provider, valid for 10 minutes. Redirect URI `<PUBLIC_BASE_URL>/admin/oauth/callback`.
3. `GET /admin/oauth/callback` verifies `state` against the current session and exchanges the code. It refuses, writing nothing, when:
   - there is no refresh token;
   - `gmail.modify` is not among the granted scopes (Google lets the user untick it);
   - the ID token's email is missing or not verified;
   - the address is already linked under another alias;
   - for `link`, the alias was taken in the meantime (for example in another browser tab).
4. On success it writes `secrets/accounts/gmail/<alias>.json` in the format `Credentials.from_authorized_user_file` reads today (token, refresh token, client id and secret, token URI, scopes): written to a temporary file in the same directory with mode 600, then renamed into place. It then calls `registry.upsert` and records the account in `accounts.json`.

The Web client's id and secret go into the credential file, as the Desktop client's do today. Credentials linked with either client keep working.

### Re-linking a mailbox

`POST /admin/accounts/<alias>/relink` runs the same flow with action `relink` for an alias that is linked or recorded as removed. The address returned must equal the one in `accounts.json`; otherwise the callback refuses and the existing file is left untouched. On success it replaces the file atomically, `upsert` drops the cached provider (and its cached credentials), and `removed_at` is cleared.

An alias with no address on record (an account linked before this design whose credential no longer works, so its address could not be recorded) can be re-linked once without the address check. The page says so, and the address is recorded from then on.

### Removing a mailbox

`GET /admin/accounts/<alias>/remove` shows a confirmation: the address and the number of changes in the journal that can no longer be undone while the account is removed. Re-linking the same mailbox under the same alias makes them undoable again, because journal entries refer to the alias and message ids.

`POST /admin/accounts/<alias>/remove` requires CSRF, re-authentication and the alias typed again, then:

1. revokes the refresh token at Google's revocation endpoint;
2. deletes the credential file;
3. calls `registry.remove(alias)`;
4. sets `removed_at` in `accounts.json`, keeping the address.

If revocation fails (network, already revoked), steps 2 to 4 still run and the page links to `https://myaccount.google.com/permissions` to revoke it by hand.

### Status page

`GET /admin` lists every account with its alias, provider, address and state (`ok`, `credential expired`, `error`), with re-link and remove buttons, plus removed aliases with a re-link button. The state comes from a cheap check per account (Gmail `users.getProfile`, 1 quota unit) run when the page loads. The same check fills in `accounts.json` for accounts linked before this design.

### Accounts directory permissions

The compose file mounts `secrets/accounts` read-write. At startup the server checks that it can create and remove a file in each provider folder. If it cannot, `/admin` shows status only, with the notice "credentials are read-only: linking is disabled", and the command-line flow keeps working. Writes to the directory happen only in the admin code path; a test checks that no MCP tool module imports it.

### Pages and browser hardening

Server-rendered HTML with a small embedded stylesheet, no JavaScript, usable on a phone. Every value (alias, address, error text) is HTML-escaped. All `/admin` responses carry:

- `Content-Security-Policy: default-src 'none'; style-src 'self'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'`
- `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`

`same-origin`, not `no-referrer`: under `no-referrer` browsers send `Origin: null` and no Referer on the page's own form POSTs, and the same-origin check refuses an opaque origin, so every action would fail. `same-origin` still sends no referrer to other sites.

### Errors

Every failure redirects to the status page with one short, specific message and no stack trace: invalid or expired `state`, consent refused, Gmail scope not granted, a different mailbox on re-link, mailbox already linked under another alias, invalid or taken alias, read-only credentials, revocation failed. Nothing is written to disk before every check has passed.

### Audit log

Admin logins (accepted and refused), re-authentications, links, re-links and removals are logged with the owner email, alias, provider and mailbox address. Tokens, codes and cookies are never logged.

### Configuration

- `ADMIN=on|off`, default `on`. With `off` the routes are not registered and answer 404.
- No new required variables: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_BASE_URL`, `ALLOWED_EMAILS` and `JWT_SIGNING_KEY` already exist.
- Google Cloud setup: add `<PUBLIC_BASE_URL>/admin/auth/callback` and `<PUBLIC_BASE_URL>/admin/oauth/callback` to the Web client's authorized redirect URIs; optionally enable the session age claim.
- The session length (8 hours) and re-authentication window (5 minutes) are constants.

## Testing

- **Unit.** Cookie signing, tampering and expiry; key derivation separate from the connector key; CSRF token and `Origin`/`Referer` checks; the 5-minute rule with and without `auth_time`; alias validation; `upsert` and `remove` while another thread holds an `Account`; atomic credential writes with mode 600; `accounts.json` reads and writes.
- **Gmail Linker** against a fake Google: authorization URL parameters, code exchange, missing refresh token, unticked Gmail scope, unverified or missing email.
- **Routes** with Starlette's test client: without a session every page redirects to login; a POST without a valid CSRF token or with a foreign `Origin` gets 403; a stale authentication forces re-authentication; a re-link returning a different mailbox is refused and the file is unchanged; a mailbox already linked under another alias is refused; remove calls revocation and deletes the file, and still removes when revocation fails; a read-only directory disables the actions; `ADMIN=off` gives 404.
- **Boundaries.** No MCP tool module imports the credential-writing code; the core/provider boundary test still passes.
- **Manual, on a real host.** Link a test mailbox, re-link it, try re-linking it with a different Google account, remove it, and confirm it disappears from `list_accounts` without a restart.

## Documentation

- Tutorial: linking moves to `/admin`, with the two redirect URIs and the session age claim; `mailhub-authorize` becomes the alternative. The Desktop client is no longer needed for new installs.
- README: an admin page entry under features and the safety model.
- Roadmap: remove the admin page from Planned (the proposals step stays); add "Login providers: Microsoft and generic OIDC" under Ideas.
