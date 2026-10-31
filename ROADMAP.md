# Roadmap

Planned and proposed work. Items move to the changelog (git history) when they ship.

## Planned

mailhub is meant to work with any MCP client, not only Claude. The server provides tools; scheduling, prompts and judgment belong to whichever model or client uses them.

- **Client-agnostic connector.** Remove what ties the server to one client:
  - **Configurable OAuth redirect URIs.** `CLAUDE_REDIRECT_URIS` only accepts claude.ai/claude.com callbacks, so other MCP clients cannot register. Replace it with `ALLOWED_REDIRECT_URIS` (default: the Claude callbacks), documented per client.
  - **Neutral organization label.** Default `mailhub/organized` instead of `claude/organized`, configurable (`ORG_LABEL`) so existing installs keep their current label without migrating messages.
  - **Neutral wording.** Tool descriptions, prompts and docs say "the model" or "the MCP client" instead of "Claude"; the tutorial gets a section on connecting other MCP clients.

- **Owner admin page (`/admin`).** A small web page on the server, behind the same Google login and allowlist as the connector (signed `HttpOnly`/`Secure`/`SameSite=Strict` session cookie, CSRF protection):
  - list linked mailboxes and their status (ok, expired credentials);
  - link a new mailbox or re-link an expired one through Google's web OAuth flow (verified `state`, redirect `/admin/oauth/callback` on the existing Web client); the token is written in place with the right owner and mode, and the account is loaded without a restart;
  - every link and removal is logged; no MCP tool can reach these actions.
  - Second step: approve or reject preference proposals from the same page, so `mailhub-admin` over SSH is no longer needed day to day.
  - Requires the token directory to be writable by the server process (today it is mounted read-only); limit writes to the admin code path.

- **Stored label rules.** Keep mail labeled as it arrives, without depending on a client's scheduling and without a model inside the server:
  - The user describes a rule in natural language; the model translates it into an exact search query and label and calls `propose_label_rule(name, description, query, label)`.
  - The owner approves out of band (`mailhub-admin`, later the admin page). No model can approve its own rule; `disable_label_rule(id)` takes effect immediately because disabling is safe.
  - `list_label_rules` shows active and pending rules and what each one changed; `apply_label_rules(accounts?)` applies approved rules on demand, for clients that schedule their own runs.
  - An optional server-side scheduler (`LABEL_RULES_INTERVAL`, e.g. hourly) applies approved rules to new mail with no model involved, so automation also works with clients that cannot schedule.
  - Every application goes through the same path as `modify_by_query`: preview-free but journaled, undoable, daily cap, organization label.
  - Rules that need judgment ("emails that need my reply") cannot be expressed as a search query; they stay with a model-driven run (a client routine calling the existing tools).
  - Comes after the admin page, which will also approve rules.

## Ideas

- **Headless account linking from the CLI.** `mailhub-authorize` with a fixed loopback port and an SSH port-forward, so accounts can be linked on the server without a local Python setup. Largely superseded by the admin page.
- **Safe attachment download.** Fixed download root with no model-chosen destination, inert filenames, refusal of the file types Gmail itself blocks and of attachments on spam-labeled messages. Would let triage read invoices and payment slips.
- **Gmail filters through approval.** The model proposes a filter, the owner approves it out of band, and the server creates it. Filters act outside the journal and undo, so this needs care.
- **Delete empty labels.** A narrow tool that removes only labels with no messages, created by mailhub, and not protected.
- **Per-account write locks.** Today one journal lock serializes writes for all accounts, so a rate-limited mailbox can delay writes on the others.
- **Search-index lag mitigation.** Take a second history marker right before a write and drop prior-state entries that history shows were no-ops.
- **Host/Origin protection.** Evaluate FastMCP's host/origin protection behind common reverse proxies and tunnels.
- **More providers.** Outlook (Microsoft Graph) and generic IMAP behind the existing `MailProvider` interface and contract suite.
- **"Alternatives" section in the README.** Point to Google's official Gmail MCP server and other open-source Gmail MCP servers, and explain when mailhub is the better fit.
