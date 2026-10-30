# Architecture

## Package layout

```
src/mailhub_mcp/
  server.py        FastMCP app: tool definitions, audit logging, entry point (mailhub-mcp)
  auth.py          Google OAuth with an email allowlist; Claude-only redirect URIs
  config.py        Settings.from_env()
  cli.py           mailhub-authorize: links an account
  admin.py         mailhub-admin: out-of-band approval of preference proposals
  core/            Provider-agnostic logic
    accounts.py    Account discovery and registry
    aggregate.py   Fan-out across accounts, per-account error isolation
    organize.py    Guarded, journaled label changes, daily cap and undo
    content.py     Sanitizing, untrusted markers and suspicion heuristics
    drafts.py      Thread participants and draft recipient rules
    preferences.py Triage preferences, proposals, approval and undo
    history.py     Journal entry summaries for list_changes
    journal.py     Append-only JSONL change journal
    models.py      Typed mail models
    errors.py      Shared exceptions
    overview.py    Inbox overview aggregation
  providers/
    base.py        MailProvider protocol, GetResult, SearchResult
    __init__.py    PROVIDERS and AUTHORIZERS registries
    gmail/         Gmail implementation and its authorize flow
```

**Dependency rule.** `core/` must not import `mailhub_mcp.providers`, `google`, `googleapiclient` or `fastmcp`. `tests/core/test_boundary.py` enforces it. Providers and the server depend on the core, never the reverse. Providers may import only `core.models`, `core.errors` and `core.content` from it.

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
| `list_drafts(limit)` / `create_draft(...)` | Drafts; replies only, to thread participants; creation never sends |

`GetResult` has three fields: `found` (the messages), `missing` (IDs that do not exist), and `failed` (ID to readable reason, for transient or unreadable errors). Callers must treat the three differently: organizing refuses to proceed if any ID is missing or failed, while reads report them in `errors`.

## Tools

All 17 are defined in `server.py`. With `TOOLS=read-only` only the ten marked read-only are registered.

| Tool | Mode | Description |
|---|---|---|
| `list_accounts` | read-only | Aliases with email, provider, query hint and credential status |
| `search_emails` | read-only | Search all or selected accounts; max 100 per account |
| `read_email` | read-only | One message in full |
| `read_emails` | read-only | Up to 25 messages of one account |
| `read_thread` | read-only | A whole thread |
| `inbox_overview` | read-only | Per-account totals, unread, newsletters, categories, top 30 senders |
| `list_drafts` | read-only | Existing drafts |
| `list_labels` | read-only | Labels of one account |
| `create_label` | write | Create or return a label |
| `modify_emails` | write | Labels, archive, read, star on specific IDs (max 1000) |
| `modify_by_query` | write | Same, by query; preview unless `confirm=true`; max 500 per call |
| `undo_change` | write | Revert a journaled change |
| `list_changes` | read-only | Journal entries (labels and preferences), newest first, with label names; filter by accounts and `since`; max 100 per call |
| `undo_since` | write | Undo every change (labels and preferences) since a time, newest first; preview unless `confirm=true`; max 50 per call |
| `create_draft` | write | Create an unsent reply draft (replies only, thread participants only; refused if any message in the thread is flagged suspicious) |
| `get_preferences` | read-only | Triage preferences: global and account rules, pending proposals, recent changes |
| `set_preferences` | write | Change preferences (needs `user_request`); lowering attention creates a proposal |

Each call is logged as `tool=<name> args=<...>` by the `AuditLog` middleware.

## Protections, journal and undo

`organize.modify` validates everything before the first write: protected labels are refused, label names resolve to IDs, `claude/organized` cannot be removed by a change (only an undo drops it), and a label cannot be both added and removed. It reads each message's current labels, then adds `claude/organized`, applies the change and appends a journal entry.

Each entry in `changes.jsonl` holds `change_id`, `timestamp`, `account`, `tool`, `added`, `removed`, `prior` (per message, only the touched labels it had), `query` and `undo_of`. `prior` makes undo exact: it removes only labels the change added to messages that did not have them, and re-adds only labels it removed from messages that had them. Undoing is itself journaled (`undo_of`), so a change cannot be undone twice. If a message has since been deleted it is skipped and listed in `skipped`. If the journal write fails after labels changed, the full entry is logged as an error for manual recovery.

`Journal.entries(since)` returns valid entries newest first by reversed file order (append order is causal, serialized by the journal lock) and skips malformed lines; `since` only filters, inclusively, by timestamp (naive times are UTC). `organize.undo_since` takes the entries since a time, drops undo entries and changes already undone, and optionally filters by account. It undoes them newest first, so two changes to the same message restore its exact original labels. Each change goes through `undo`, so every protection still applies and the journal lock is taken per change, not across the loop. If a change fails (reported in `failed`), older candidates touching any of its messages are held back and also reported in `failed` as blocked, so retrying the failed one later still restores exactly. A call attempts candidates until 50 are undone (`MAX_UNDO_SINCE`) or 200 were attempted (`MAX_UNDO_SINCE_ATTEMPTS`), so persistent failures cannot starve healthy changes; `remaining` says whether candidates were left unattempted. Without `confirm` it only returns summaries (up to 50) and writes nothing.

`modify_by_query` skips messages already in the target state and returns a hint when nothing is left.

## Content defenses

Every email is text written by a third party, so what reaches Claude is sanitized, delimited and assessed. `core/content.py` is provider-agnostic; the Gmail provider does the sanitizing and the server (`server.py`) does the delimiting and the assessment, for every tool that returns messages (search, read, thread and drafts).

**What is removed.** `html_to_safe_text(html)` returns the visible text and a count of removed hidden characters. It drops `<head>`, `<script>`, `<style>`, `<template>`, `<noscript>`, comments, elements hidden by inline style (`display:none`, `visibility:hidden`, `opacity:0`, a font size of zero or tiny, `max-height:0` with `overflow:hidden`) and elements with the `hidden` attribute or `aria-hidden="true"`. It does not compare colors. `clean_text(s)` removes zero-width and bidirectional control characters, Unicode tag characters, other control characters except newline and tab, and collapses runs of blank lines. The Gmail provider applies `html_to_safe_text` and `clean_text` to the body and `clean_text` to subject, sender, recipients, date, snippet, attachment names and attachment MIME types.

**Markers.** Each tool response uses one random nonce of 12 hex characters. `body` and `snippet` are wrapped as `[untrusted:<nonce>]` and `[/untrusted:<nonce>]` on their own lines. Any `[untrusted:` or `[/untrusted:` already inside the text is rewritten to `[untrusted_:` and `[/untrusted_:` first, so an email cannot forge a closing marker. The descriptions of the read tools explain the markers.

**Flags.** Each message gets `suspicious` and `suspicious_reasons`, computed by `assess` from the sender-controlled text fields (subject, From, To, Cc, Reply-To, snippet, body and attachment names) and the hidden-text count. Reasons: `mentions_tools` (the name of any mailhub-mcp tool, in any mode), `instruction_override` (for example "ignore previous instructions", in English and Portuguese), `addresses_assistant`, `requests_action` (forward, create a draft, change preferences or settings, "reply to ... with") and `hidden_text` (more than 200 hidden characters removed). The flags are a heuristic aid, not a guarantee. Because they are computed in the server, they are also what `create_draft` uses to refuse a suspicious thread.

## Preferences

`core/preferences.py` stores triage preferences in `preferences.json` next to the journal (`JOURNAL_PATH`). There are two kinds of scope: `global` and one per account alias; the effective rules for an account are the global rules plus its own. Fields: `important_senders`, `ignored_senders` (`name@domain` or `@domain`, at most 254 characters), `important_keywords`, `ignored_keywords` (1-40 characters, NFC-normalized, only letters of any script, digits, spaces, apostrophes, dots and hyphens), each list capped at 200 items, and `last_triage_at` (ISO 8601, not in the future). A value cannot be both important and ignored in a scope. Keywords are short and restricted, but they are still text written by the model and shown back to it, so when tools return preferences every sender pattern and keyword (like every `user_request`) is wrapped in untrusted markers; only timestamps, ids, scopes and field names are not. The stored values stay raw.

**Direction rule.** Adding to an important list, or removing from an ignored list, raises attention and applies immediately. Adding to an ignored list, or removing from an important list, lowers attention and becomes a proposal. `last_triage_at` is set immediately. Each `set_preferences` call needs a `user_request` (1-500 characters), stored with the change and shown by `get_preferences` and `list_changes`; it is wrapped in untrusted markers when shown.

**Proposals.** A proposal lives in `preferences.json` under `proposals` and is never applied by an MCP tool. It expires after 14 days; expired proposals are dropped on read. An identical open proposal is reused rather than duplicated, and at most 50 can be open at once. `mailhub-admin preferences list|show|approve|reject <id>` is the only way to resolve one. Approval validates it again against the current state, applies it and journals a change with tool `approve_preference`; rejection journals nothing. `mailhub-admin` reads only `JOURNAL_PATH` (default `/data/changes.jsonl`), needs no OAuth settings, and exits 0 on success and 2 on any error.

**Store locking.** The server and the CLI both write `preferences.json`. Writes hold an exclusive `fcntl.flock` on `preferences.lock` and replace the file atomically (temporary file, `fsync`, `os.replace`).

**Journal and undo.** Preference changes are journal entries with `kind: "preferences"`, plus `scope`, `before` and `after` per touched field and `user_request`; entries without a `kind` are label changes. `list_changes` shows both. `undo_change` on a preference entry restores `before` under the direction rule: the parts that raise attention (un-ignoring, re-marking important) and `last_triage_at` apply at once as one journaled undo, while the parts that would lower attention (removing values from `important_*`, adding values to `ignored_*`) become proposals with `user_request` "undo of <change_id>: <original user_request>", returned in `proposed` and approved with `mailhub-admin` like any other. When nothing applies, no undo entry is written (`change_id` is null) and the change stays undoable; repeating it reuses the same proposals. Undo entries themselves cannot be undone ("this change is an undo; to redo, make the change again"), for label and preference entries alike, so a redo always goes through the cap and the direction rule. `undo_since` includes preference entries in the same newest-first order, but holds changes back by `(scope, field)` instead of `(account, message_id)`; a preference change whose undo only produced proposals is listed under `proposed`, not counted as undone, and does not hold anything back by itself. Undo is refused, and nothing is changed, if a touched field no longer equals `after`: a later change is never overwritten. Double undo is refused.

## Capability limits

- **Drafts.** `create_draft` requires `reply_to_message_id`. Participants are every address in From, To, Cc and Reply-To across the thread, plus the account's own address; `to` and `cc` outside that set are refused and the error lists the allowed addresses. `to` defaults to the Reply-To of the replied message, else its From. A thread containing any message flagged suspicious is refused. Nothing is ever sent.
- **Daily cap.** `MAX_CHANGES_PER_DAY` (default 2000) limits messages changed by `modify_emails` and `modify_by_query` per account in a rolling 24 hours, counted from the journal (undo entries excluded). The check runs under the journal lock, before any write, and the error ("would exceed the daily change cap") says how many changes remain. Undo is never capped.
- **Read-only mode.** `TOOLS=all|read-only` (default `all`). In `read-only` only `list_accounts`, `search_emails`, `read_email`, `read_emails`, `read_thread`, `inbox_overview`, `list_drafts`, `list_labels`, `list_changes` and `get_preferences` are registered; the write tools do not exist on that instance. Any other value fails startup. Since `set_preferences` is not available either, such an instance cannot record `last_triage_at`.

## Limitations

- **The server cannot verify who motivated a call.** `user_request` is text the model supplies, and a manipulated model can write a plausible one. The real check is you reviewing proposals with `mailhub-admin preferences show` before approving.
- **Heuristics can miss attacks.** Suspicion flags are regex-based and can be evaded by paraphrase, other languages or crafted text. The suspicious-thread refusal in `create_draft` inherits that blind spot. Hidden-text removal covers explicit hiding styles, not color tricks.
- **Preferences never hide mail.** Search, read and overview ignore preferences. Rules only guide how Claude ranks and reports, so an approved or injected "ignore" can at worst lower a message in a triage report; it can never make the message unfindable.
- **Raising-attention changes apply immediately and unreviewed.** Injected text can mark an attacker's sender important (noise or priority). This is visible in `list_changes` and undoable, but undoing it lowers attention, so the undo itself is a proposal you approve with `mailhub-admin`. Undo can lower attention only through proposals.
- **Only some text is delimited.** `body`, `snippet`, `user_request`, preference values and the `query` of a recorded label change are wrapped in untrusted markers. Subjects, names, addresses and attachment names are cleaned of invisible characters and fed to the suspicion heuristics, but they are not delimited.

## Adding a provider

1. Create `providers/<name>/` with a class implementing `MailProvider`, constructed as `(alias, credential_path)`, and `authorize(client_secret: Path) -> str` returning the credential file contents.
2. Run the shared contract suite in `tests/providers/contract.py` against it (see how the Gmail tests parametrize it).
3. Register the class in `PROVIDERS` and the function in `AUTHORIZERS` in `providers/__init__.py`. A test requires both to have the same keys.
4. Link accounts with `mailhub-authorize <name> <client_secret> <alias>`; credentials land in `secrets/accounts/<name>/<alias>.json`.
