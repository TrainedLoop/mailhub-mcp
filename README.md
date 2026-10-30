# mailhub-mcp

An MCP server that exposes several mail accounts to Claude as a [custom connector](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp) in claude.ai. Gmail is supported today; other providers plug in through a small provider interface (see [docs/architecture.md](docs/architecture.md)).

It is built for inbox triage: Claude can search, read and summarize mail across all your accounts, then tidy up with labels, archiving and drafts. Every organizing change is reversible, and the server has no way to send, trash or delete mail.

## Features

- **Multi-account**: each mailbox gets an alias. Read tools take `accounts=None` to mean "all accounts" and return `{results, errors}`, with at most one error per account.
- **Search, read and triage signals**: `search_emails`, `read_email`, `read_emails`, `read_thread`, `list_labels`, `list_drafts`. Messages carry unread, starred, importance, category and newsletter flags.
- **Inbox overview**: `inbox_overview` gives per-account totals, unread counts, newsletters, categories and top senders.
- **Reversible organization**: `create_label`, `modify_emails`, `modify_by_query` (labels, archive, mark read, star), each recorded in a journal, `undo_change` to revert a batch, `list_changes` to find earlier changes and their change_ids across sessions, and `undo_since` as a panic button that undoes everything since a time.
- **Drafts**: `create_draft` leaves a draft in the mailbox for you to review and send yourself.

## Safety model

- The code never calls `messages.trash`, `messages.delete`, `batchDelete`, `messages.send` or `drafts.send`. A test scans `src/` to enforce this.
- The system labels `TRASH`, `SPAM`, `DRAFT`, `SENT` and `CHAT` are refused in `add_labels` and `remove_labels`, by name or by ID.
- `modify_by_query` previews by default (`confirm=false`: match count plus a sample). Applying (`confirm=true`) touches at most 500 messages per call.
- Every change is journaled to `changes.jsonl` with the labels each message had before. `undo_change` reverts exactly that batch and never removes labels a message already had.
- `list_changes` shows the journal (newest first, with label names). `undo_since` reverts every change since a time, newest first so overlapping changes restore exactly; it previews by default (`confirm=false`) and handles at most 50 changes per call.
- Every message the server changes gets the `claude/organized` label (created automatically in each account), so you can always find what Claude touched. `modify_emails` and `modify_by_query` refuse to remove it; only `undo_change` takes it off, together with the rest of the reverted change.
- Email content is treated as untrusted third-party text; tool descriptions tell the model not to follow instructions found inside messages.
- Access to the server requires a Google login, restricted to the addresses in `ALLOWED_EMAILS`, and checked on every request. Only Claude's own OAuth redirect URIs can register as clients.

Note on scope: Gmail's `gmail.modify` scope technically also permits sending and trashing, and it is the minimum that covers label changes and drafts. The protections above are in this code, not in the token. Review `src/` if that distinction matters to you.

## Quickstart

1. Follow [docs/tutorial.md](docs/tutorial.md) for the Google Cloud setup, account linking, configuration and the claude.ai connector.
2. Copy `.env.example` to `.env`, fill it in, and run `docker compose up -d --build`. The server listens on `127.0.0.1:8742` (set `PORT` to change it; keep the compose port mapping in sync); it needs HTTPS at a public URL in front of it.
3. [examples/tailscale-funnel/](examples/tailscale-funnel/) shows one complete way to publish it, including a firewall that isolates the container.

## Development

```bash
uv sync
uv run pytest -q
```

Layout and extension points are described in [docs/architecture.md](docs/architecture.md).

## License

MIT, see [LICENSE](LICENSE).
