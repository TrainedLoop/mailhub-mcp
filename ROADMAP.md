# Roadmap

Planned and proposed work. Items move to the changelog (git history) when they ship.

## Planned

mailhub is meant to work with any MCP client, not only Claude. The server provides tools; scheduling, prompts and judgment belong to whichever model or client uses them.

- **Approve preference proposals from the admin page.** Approve or reject preference proposals on `/admin`, behind the same owner sign-in and recent re-authentication as linking, so `mailhub-admin` over SSH is no longer needed day to day.

- **Stored label rules.** Keep mail labeled as it arrives, without depending on a client's scheduling and without a model inside the server:
  - The user describes a rule in natural language; the model translates it into an exact search query and label and calls `propose_label_rule(name, description, query, label)`.
  - The owner approves out of band (`mailhub-admin`, later the admin page). No model can approve its own rule; `disable_label_rule(id)` takes effect immediately because disabling is safe.
  - `list_label_rules` shows active and pending rules and what each one changed; `apply_label_rules(accounts?)` applies approved rules on demand, for clients that schedule their own runs.
  - An optional server-side scheduler (`LABEL_RULES_INTERVAL`, e.g. hourly) applies approved rules to new mail with no model involved, so automation also works with clients that cannot schedule.
  - Every application goes through the same path as `modify_by_query`: preview-free but journaled, undoable, daily cap, organization label.
  - Rules that need judgment ("emails that need my reply") cannot be expressed as a search query; they stay with a model-driven run (a client routine calling the existing tools).
  - Comes after proposal approval on the admin page, which will also approve rules.

## Follow-ups

Known gaps from the review of the admin page (v0.2.0). None of them can attach the wrong mailbox or lose data; they are worth closing before the next feature.

- **Refuse tools on a "different mailbox" alias.** When a credential placed by hand or by the CLI belongs to another mailbox than the one on record, `/admin` flags it, but the MCP tools still serve it. Tools should refuse that alias until it is re-linked.
- **Context-aware credential hint.** The "invalid or revoked credentials" message suggests re-linking on `/admin` even when `ADMIN=off` or the credentials folder is read-only; it should point to the command line in those cases.
- **Stricter one-time re-link.** If the old credential's address probe fails for a reason other than expired credentials (a timeout, a busy server), the re-link should not fall back to the one-time unchecked path.
- **Record every address at startup.** The account book learns legacy accounts' addresses only when the status page loads; recording them at startup would let the "already linked under another alias" check see every account.
- **Status checks under load.** Admin network calls share a 4-slot limiter, and a check's 10-second timeout includes waiting for a slot; many slow mailboxes can show a spurious `error`.
- **Validate scheduled routines.** Confirm a scheduled routine in claude.ai can use the connector end to end (and document any setup it needs) before the docs or the site mention it.

## Ideas

- **Safe attachment download.** Fixed download root with no model-chosen destination, inert filenames, refusal of the file types Gmail itself blocks and of attachments on spam-labeled messages. Would let triage read invoices and payment slips.
- **Gmail filters through approval.** The model proposes a filter, the owner approves it out of band, and the server creates it. Filters act outside the journal and undo, so this needs care.
- **Delete empty labels.** A narrow tool that removes only labels with no messages, created by mailhub, and not protected.
- **Per-account write locks.** Today one journal lock serializes writes for all accounts, so a rate-limited mailbox can delay writes on the others.
- **Search-index lag mitigation.** Take a second history marker right before a write and drop prior-state entries that history shows were no-ops.
- **Host/Origin protection.** Evaluate FastMCP's host/origin protection behind common reverse proxies and tunnels.
- **More providers.** Outlook (Microsoft Graph) and generic IMAP behind the existing `MailProvider` interface and contract suite.
- **"Alternatives" section in the README.** Point to Google's official Gmail MCP server and other open-source Gmail MCP servers, and explain when mailhub is the better fit.
- **Login providers.** Microsoft and generic OIDC (covers self-hosted identity such as Pocket ID or Authelia) for the MCP connector and the admin page together.
- **Quarantine label for deletion candidates.** A tool (or a mode of `modify_by_query`) that applies `mailhub/to-trash` and archives, journaled and undoable like any label change. The owner opens that label in their mail client and deletes in bulk. The model sorts; the person deletes.
- **Optional trash and spam (`ALLOW_TRASH=on`, off by default).** Move messages to Trash or mark them as spam, always previewed and explicitly confirmed, journaled, with its own smaller daily cap. Refuses starred, important and suspicious-flagged messages. Responses warn that undo only works while the provider keeps the message (Gmail empties Trash and Spam after 30 days), and that marking as spam also trains the provider's filter, which undo cannot reverse. The default stays "cannot delete"; the forbidden-calls test keeps `delete`/`batchDelete` banned even with the option on.
