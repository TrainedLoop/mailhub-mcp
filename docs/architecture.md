# Architecture

## Package layout

```
src/mailhub_mcp/
  server.py        FastMCP app: tool definitions, audit logging, entry point (mailhub-mcp)
  auth.py          Google OAuth with an email allowlist; Claude-only redirect URIs
  config.py        Settings.from_env()
  cli.py           mailhub-authorize: links an account
  core/            Provider-agnostic logic
    accounts.py    Account discovery and registry
    aggregate.py   Fan-out across accounts, per-account error isolation
    organize.py    Guarded, journaled label changes and undo
    journal.py     Append-only JSONL change journal
    models.py      Typed mail models
    errors.py      Shared exceptions
    overview.py    Inbox overview aggregation
  providers/
    base.py        MailProvider protocol, GetResult, SearchResult
    __init__.py    PROVIDERS and AUTHORIZERS registries
    gmail/         Gmail implementation and its authorize flow
```

**Dependency rule.** `core/` must not import `mailhub_mcp.providers`, `google`, `googleapiclient` or `fastmcp`. `tests/core/test_boundary.py` enforces it. Providers and the server depend on the core, never the reverse.

## Models

`core/models.py` defines pydantic models: `MessageSummary` (id, thread_id, from, to, subject, date, snippet, labels, internal_date, unread, starred, is_important, category, is_newsletter), `Message` (adds cc, body, attachments), `Thread`, `Label`, `Draft`, `DraftSummary`, `Attachment`, `LabelState`. Models that report their origin carry an `account` alias, stamped by `with_account` or by the fan-out.

## Errors

`core/errors.py`: `MailhubError` is the base. `AccountNotFound`, `DuplicateAlias`, `AuthExpired` (carries the re-authorize command), `MessageNotFound`, `TransientError`, and `OrganizeError` (a `ValueError`) for refused changes. The server turns them into `ToolError`s; aggregated tools put per-account failures in `errors` instead.

## Accounts

`AccountRegistry` scans `<accounts_dir>/<provider>/<alias>.json`. The folder name picks the provider; the file name (without `.json`) is the alias and must match `^[a-z0-9][a-z0-9-]{0,31}$`. Unknown provider folders, loose files and invalid aliases are skipped with a warning. The same alias under two providers raises `DuplicateAlias` at startup. Providers are built lazily on first use, so a bad credential file affects only its account. Accounts are read once at startup.

## The MailProvider interface

`providers/base.py` defines a `Protocol`.

Class attributes: `name`, `protected_labels` (names that may never be changed), `inbox_label`, `unread_label`, `starred_label`, `default_inbox_query`, `query_hint` (search syntax shown to the model), `reauthorize_hint` (command template with `{alias}`).

Methods:

| Method | Purpose |
|---|---|
| `address()` | The mailbox's email address |
| `search(query, limit)` / `search_report(query, limit)` | Matching summaries; the report variant also counts unreadable messages |
| `list_ids(query, limit)` | Matching message IDs only |
| `get_messages(ids, detail)` | Fetch with `detail` of `"summary"`, `"full"` or `"labels"`; returns a `GetResult` |
| `get_thread(thread_id)` | Thread with messages oldest first |
| `list_labels()` / `ensure_label(name)` | Labels; create-or-return by name, case-insensitive |
| `modify_labels(ids, add, remove)` | Apply label IDs |
| `list_drafts(limit)` / `create_draft(...)` | Drafts; creation never sends |

`GetResult` has three fields: `found` (the messages), `missing` (IDs that do not exist), and `failed` (ID to readable reason, for transient or unreadable errors). Callers must treat the three differently: organizing refuses to proceed if any ID is missing or failed, while reads report them in `errors`.

## Tools

All 13 are defined in `server.py`.

| Tool | Description |
|---|---|
| `list_accounts` | Aliases with email, provider, query hint and credential status |
| `search_emails` | Search all or selected accounts; max 100 per account |
| `read_email` | One message in full |
| `read_emails` | Up to 25 messages of one account |
| `read_thread` | A whole thread |
| `inbox_overview` | Per-account totals, unread, newsletters, categories, top 30 senders |
| `list_drafts` | Existing drafts |
| `list_labels` | Labels of one account |
| `create_label` | Create or return a label |
| `modify_emails` | Labels, archive, read, star on specific IDs (max 1000) |
| `modify_by_query` | Same, by query; preview unless `confirm=true`; max 500 per call |
| `undo_change` | Revert a journaled change |
| `create_draft` | Create an unsent draft, optionally a reply |

Each call is logged as `tool=<name> args=<...>` by the `AuditLog` middleware.

## Protections, journal and undo

`organize.modify` validates everything before the first write: protected labels are refused, label names resolve to IDs, `claude/organized` cannot be removed by a change (only an undo drops it), and a label cannot be both added and removed. It reads each message's current labels, then adds `claude/organized`, applies the change and appends a journal entry.

Each entry in `changes.jsonl` holds `change_id`, `timestamp`, `account`, `tool`, `added`, `removed`, `prior` (per message, only the touched labels it had), `query` and `undo_of`. `prior` makes undo exact: it removes only labels the change added to messages that did not have them, and re-adds only labels it removed from messages that had them. Undoing is itself journaled (`undo_of`), so a change cannot be undone twice. If a message has since been deleted it is skipped and listed in `skipped`. If the journal write fails after labels changed, the full entry is logged as an error for manual recovery.

`modify_by_query` skips messages already in the target state and returns a hint when nothing is left.

## Adding a provider

1. Create `providers/<name>/` with a class implementing `MailProvider`, constructed as `(alias, credential_path)`, and `authorize(client_secret: Path) -> str` returning the credential file contents.
2. Run the shared contract suite in `tests/providers/contract.py` against it (see how the Gmail tests parametrize it).
3. Register the class in `PROVIDERS` and the function in `AUTHORIZERS` in `providers/__init__.py`. A test requires both to have the same keys.
4. Link accounts with `mailhub-authorize <name> <client_secret> <alias>`; credentials land in `secrets/accounts/<name>/<alias>.json`.
