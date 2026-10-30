# Roadmap

Planned and proposed work. Items move to the changelog (git history) when they ship.

## Next (in progress)

- **`count_messages` tool.** Count how many messages match a query, in one account or all of them, from message ids only (`messages.list`, about 5 quota units per 500 messages). Lets the model size a bulk change or split it into steps before spending quota on reads.
- **Short message bodies by default.** `read_email`, `read_emails` and `read_thread` return the first part of each body (a small default, with a `max_body_chars` argument to ask for more or the full text). Triage rarely needs whole bodies; shorter bodies mean less context and less surface for prompt injection.
- **One untrusted fence per response.** Wrap all email-derived text of a response in a single delimited block and keep message ids in a trusted manifest outside it, instead of wrapping every body and snippet separately. Fewer tokens, and an id forged inside an email is never in the trusted region.
- **Document the "lethal trifecta".** Explain the risk model (private data + untrusted content + an egress channel) in the README and `docs/architecture.md`, and that pairing mailhub with another tool that can reach the internet in the same session reopens an egress path the server cannot control.

## Planned

- **Owner admin page (`/admin`).** A small web page on the server, behind the same Google login and allowlist as the connector (signed `HttpOnly`/`Secure`/`SameSite=Strict` session cookie, CSRF protection):
  - list linked mailboxes and their status (ok, expired credentials);
  - link a new mailbox or re-link an expired one through Google's web OAuth flow (verified `state`, redirect `/admin/oauth/callback` on the existing Web client); the token is written in place with the right owner and mode, and the account is loaded without a restart;
  - every link and removal is logged; no MCP tool can reach these actions.
  - Second step: approve or reject preference proposals from the same page, so `mailhub-admin` over SSH is no longer needed day to day.
  - Requires the token directory to be writable by the server process (today it is mounted read-only); limit writes to the admin code path.

## Ideas

- **Headless account linking from the CLI.** `mailhub-authorize` with a fixed loopback port and an SSH port-forward, so accounts can be linked on the server without a local Python setup. Largely superseded by the admin page.
- **Safe attachment download.** Fixed download root with no model-chosen destination, inert filenames, refusal of the file types Gmail itself blocks and of attachments on spam-labeled messages. Would let triage read invoices and payment slips.
- **Stored label rules.** Keep approved label rules on the server (proposed by the model, approved out of band like lowering preferences) and an `apply_label_rules` tool, so a daily routine keeps new mail labeled without the rules living in the routine's prompt.
- **Gmail filters through approval.** The model proposes a filter, the owner approves it out of band, and the server creates it. Filters act outside the journal and undo, so this needs care.
- **Delete empty labels.** A narrow tool that removes only labels with no messages, created by mailhub, and not protected.
- **Per-account write locks.** Today one journal lock serializes writes for all accounts, so a rate-limited mailbox can delay writes on the others.
- **Search-index lag mitigation.** Take a second history marker right before a write and drop prior-state entries that history shows were no-ops.
- **Host/Origin protection.** Evaluate FastMCP's host/origin protection behind common reverse proxies and tunnels.
- **More providers.** Outlook (Microsoft Graph) and generic IMAP behind the existing `MailProvider` interface and contract suite.
- **"Alternatives" section in the README.** Point to Google's official Gmail MCP server and other open-source Gmail MCP servers, and explain when mailhub is the better fit.
