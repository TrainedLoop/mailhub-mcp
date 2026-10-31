# Architecture

## Package layout

```
src/mailhub_mcp/
  server.py        FastMCP app: tool definitions, audit logging, entry point (mailhub-mcp)
  auth.py          Google OAuth with an email allowlist; configurable redirect URI allowlist
  config.py        Settings.from_env() and startup validation of its values
  cli.py           mailhub-authorize: links an account
  admin.py         mailhub-admin: out-of-band approval of preference proposals, `accounts` health check
  prompts.py       Text of the MCP prompts (registered in server.py)
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

`core/models.py` defines pydantic models: `MessageSummary` (id, thread_id, from, to, subject, date, snippet, labels, internal_date, unread, starred, is_important, category, is_newsletter, reply_to, unsubscribe), `Message` (adds cc, body, attachments), `Thread`, `Label`, `Draft`, `DraftSummary`, `Attachment`, `LabelState`. Models that report their origin carry an `account` alias, stamped by `with_account` or by the fan-out.

## Errors

`core/errors.py`: `MailhubError` is the base. `AccountNotFound`, `DuplicateAlias`, `AuthExpired` (carries the re-authorize command), `MessageNotFound`, `TransientError`, and `OrganizeError` (a `ValueError`) for refused changes. The server turns them into `ToolError`s; aggregated tools put per-account failures in `errors` instead.

## Ownership model

One instance serves one owner. `ALLOWED_EMAILS` is meant to hold only that person's Google login; every linked mailbox (`<accounts_dir>/<provider>/<alias>.json`) belongs to them, and the preferences, journal and undo history are per instance, not per login. Nothing separates two logins inside an instance: every allowed login can read and change every linked mailbox. `server.py` therefore logs a warning at startup when `ALLOWED_EMAILS` has more than one address. Several people are served by several instances, each with its own directory, `.env`, accounts, `data/`, container name, local port (`HOST_PORT`), Compose project name and public URL, and each person adds their own connector.

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
| `list_ids(query, limit, label_ids=None)` | Matching message IDs only, newest first; with `label_ids`, only messages carrying every one of them |
| `get_messages(ids, detail)` | Fetch with `detail` of `"summary"`, `"full"` or `"labels"`; returns a `GetResult` |
| `get_thread(thread_id)` | Thread with messages oldest first |
| `list_labels()` / `ensure_label(name)` | Labels; create-or-return by name, case-insensitive |
| `modify_labels(ids, add, remove)` | Apply label IDs |
| `list_drafts(limit)` / `create_draft(...)` | Drafts; replies only, to thread participants; creation never sends |

`GetResult` has three fields: `found` (the messages), `missing` (IDs that do not exist), and `failed` (ID to readable reason, for transient or unreadable errors). Callers must treat the three differently: organizing refuses to proceed if any ID is missing or failed, while reads report them in `errors`.

Gmail limits each user to 6,000 quota units per minute over a moving window for Cloud projects created since May 2026 (15,000 for older ones). Costs per call: `messages.get` 20 (`metadata`/`minimal`) or 60 (`full`), `messages.list` 5, `threads.get` 120 (`full`), `messages.batchModify` 50, `labels.list` 1, `getProfile` 1, `history.list` 2, `drafts.list` 5; `drafts.get` 20, `labels.create` 5 and `drafts.create` 10 are assumptions (not in Google's table). Each call inside a batch counts. The Gmail provider spends a budget of `GMAIL_UNITS_PER_MINUTE` per mailbox (default 4800, 80% of 6,000; set 12000 on a project with the older limit), through one rate limiter shared by every call on that account (reads, listings, label and draft calls, writes). At the default that is 80 units per second: about 4 metadata reads per second, so `inbox_overview` over 200 messages takes about 50 seconds and over 500 about 2 minutes. There is one retry layer: the client library's own retries are off (`num_retries=0`) and `api._execute` (single requests) and `api._run_batches` (per message in a batch) retry 5xx and network errors after 1, 2 and 4 seconds, and rate limits after 2, 4, 8, 16 and 32 seconds, each wait plus up to 1 second of random jitter; every attempt is charged to the limiter. A failure of a whole batch request goes through the same schedules. `labels.create` is retried only on rate limits (after a 5xx the label may already exist), and a 409 conflict lists the labels again and returns the existing one. A rate limit is any 429 (including "Too many concurrent requests for user") or a 403 whose reason, read from the library's `error_details` and from both `error.errors` and `error.details` of the body, is `rateLimitExceeded`, `userRateLimitExceeded` or `RATE_LIMIT_EXCEEDED` (case-insensitive); `dailyLimitExceeded` and other 403s fail at once. Draft creation is never retried, because a retry could duplicate a draft.

## Tools

All 18 are defined in `server.py`. With `TOOLS=read-only` only the eleven marked read-only are registered.

| Tool | Mode | Description |
|---|---|---|
| `list_accounts` | read-only | Aliases with email, provider, query hint and credential status |
| `search_emails` | read-only | Search all or selected accounts; max 100 per account |
| `read_email` | read-only | One message; body cut to `max_body_chars` (default 2000) |
| `read_emails` | read-only | Up to 25 messages of one account |
| `read_thread` | read-only | A whole thread |
| `inbox_overview` | read-only | Per-account totals, unread, newsletters, categories, top 30 senders; scans 200 messages by default (max 2000) |
| `count_messages` | read-only | Matches of a query per account and in total, from message ids only; stops at 10,000 per account (`capped`) |
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

**Quota in tools.** The tool descriptions tell the calling model what each call costs: about 20 units per message for `search_emails`, `inbox_overview`, `modify_emails`, `undo_change` and `undo_since` (the last three read each message first), about 60 per message for `read_email`/`read_emails` (full format), about 120 per `read_thread`, about 140 for `create_draft` (message lookup and thread read), about 5 per 500 messages for `count_messages` (id listing only), and a few units per page plus 50 per apply for `modify_by_query`, plus 20 per message when a truncated label listing leaves some unsettled or another client changed it meanwhile. `list_accounts` states the per-mailbox budget (`GMAIL_UNITS_PER_MINUTE`), and `inbox_overview` warns against parallel or back-to-back runs.

Every response that touched one or more mailboxes carries a top-level `quota` key: `{"<alias>": {"used_last_minute": int, "budget_per_minute": int}}`, next to the existing keys. `used_last_minute` is the units the mailbox's rate limiter booked in the last 60 seconds (`RateLimiter.used_last_minute`, a deque pruned on access), capped at the budget because slots booked ahead for waiting calls count too, and `budget_per_minute` the configured budget. Providers expose it through the optional `quota_status()`; the server skips providers without it. Only numbers are reported.

When the backoff gives up on a rate limit, the error is actionable instead of "temporary failure": `"<alias>: Gmail rate limit reached for this mailbox; wait about <N> seconds and retry; avoid parallel calls on the same mailbox"`, where N is the time until the limiter's booked budget frees up, clamped to 30-60 seconds (`core.errors.RateLimited`). It appears as the `ToolError` of single-account tools (including `modify_emails`, `modify_by_query` and `undo_change`, which then write nothing), as the account's entry in `errors` for multi-account tools and `search_emails`/`inbox_overview` reads, as the per-message reason in `read_emails`, and in `failed` for `undo_since`.

## Protections, journal and undo

`organize.modify` validates everything before the first write: protected labels are refused, label names resolve to IDs, `claude/organized` cannot be removed by a change (only an undo drops it), and a label cannot be both added and removed. It reads each message's current labels, then adds `claude/organized`, applies the change and appends a journal entry.

Each entry in `changes.jsonl` holds `change_id`, `timestamp`, `account`, `tool`, `added`, `removed`, `prior` (per message, only the touched labels it had), `query` and `undo_of`. `prior` makes undo exact: it removes only labels the change added to messages that did not have them, and re-adds only labels it removed from messages that had them. Undoing is itself journaled (`undo_of`), so a change cannot be undone twice. If a message has since been deleted it is skipped and listed in `skipped`. If the journal write fails after labels changed, the full entry is logged as an error for manual recovery.

`Journal.entries(since)` returns valid entries newest first by reversed file order (append order is causal, serialized by the journal lock) and skips malformed lines; `since` only filters, inclusively, by timestamp (naive times are UTC). `organize.undo_since` takes the entries since a time, drops undo entries and changes already undone, and optionally filters by account. It undoes them newest first, so two changes to the same message restore its exact original labels. Each change goes through `undo`, so every protection still applies and the journal lock is taken per change, not across the loop. If a change fails (reported in `failed`), older candidates touching any of its messages are held back and also reported in `failed` as blocked, so retrying the failed one later still restores exactly. A call attempts candidates until 50 are undone (`MAX_UNDO_SINCE`) or 200 were attempted (`MAX_UNDO_SINCE_ATTEMPTS`), so persistent failures cannot starve healthy changes; `remaining` says whether candidates were left unattempted. Without `confirm` it only returns summaries (up to 50) and writes nothing.

When an undo of a preference change is split (parts that raise attention apply at once, parts that lower it become proposals), approving any one of its proposals marks the original change undone. Approving one proposal therefore does not undo the others, and `undo_since` will not revisit the change: the remaining parts must each be approved before they expire (14 days) or be re-proposed manually with `set_preferences`.

`modify_by_query` skips messages already in the target state and returns a hint when nothing is left. It learns label state without reading each message, and without assuming any listing order: under the journal lock it lists the query (up to 501 ids), then lists the query once per touched label (the labels added or removed, plus `claude/organized` when it exists) with `label_ids=[label]`, up to 2000 ids or one more than the matched ids, whichever is larger. An id in a label's listing carries that label. A listing that returned fewer ids than its limit is the complete set, so an id absent from it does not carry the label. When a listing hit its limit, the batch ids it did not prove are read one by one (20 units each) and their state for that label comes from the read; a missing or unreadable id refuses the change before any write, as in `modify_emails`. Finally the query is listed again: a complete listing only speaks for ids that still match, so if the matches changed meanwhile (new mail, or a message leaving the query) the whole batch is read instead. That check alone misses a message that leaves the query during a label listing and returns before the re-list (another client marks it read, then unread again, under `is:unread`): the complete listing would record it without a label it has, and undo would strip that label. So the provider's optional change detection brackets the listings: `change_marker()` is taken before the query is first listed, and after the re-list `changed_since(marker, batch ids)` names the batch messages that had a label change, or were added or deleted, since then; those are read one by one (20 units each) and their state for every touched label comes from the read. On Gmail the marker is the `historyId` of `users.getProfile` (1 unit) and the check pages `users.history.list` from it (2 units per page of 500 records, `labelAdded`, `labelRemoved`, `messageAdded` and `messageDeleted`), both charged to the mailbox's limiter; when nothing changed meanwhile, the check reads no messages. When change detection is unknown (a provider without it, an expired history (404), more than 10 pages of history, or any failure after the retries other than a rate limit) the whole batch is read instead. A rate limit on either call fails the tool with the rate-limit advice before any write: reading the whole batch would spend up to 10,000 more units of a throttled budget. This assumes Gmail's search index (behind `messages.list`) keeps up with label changes: a change made just before the marker but not yet indexed would be missed by both the listing and the history check. The ids exist because they were just listed. The result drives the target-state filter, the daily cap and `prior`, exactly as a per-message read would. A 500-message change thus usually costs a few list calls (5 units per page of 500), a few units for the history check and 50 for the write, instead of 10,000 units of reads. `modify_emails` and `undo` still read every message (20 units each): they must prove explicit ids exist in the account and skip deleted ones, and they are smaller and rarer.

The journal lock is held from the first read to the journal record, so on a throttled mailbox it also waits out the backoff (up to about a minute) and other organize calls, on any account, wait with it. The lock is what keeps `prior` exact and the cap atomic, so it is not split per account.

## Content defenses

Every email is text written by a third party, so what reaches Claude is sanitized, delimited and assessed. `core/content.py` is provider-agnostic; the Gmail provider does the sanitizing and the server (`server.py`) does the delimiting and the assessment, for every tool that returns messages (search, read, thread and drafts).

**What is removed.** `html_to_safe_text(html)` returns the visible text and a count of removed hidden characters. It drops `<head>`, `<script>`, `<style>`, `<template>`, `<noscript>`, comments, elements hidden by inline style (`display:none`, `visibility:hidden`, `opacity:0`, a font size of zero or tiny, `max-height:0` with `overflow:hidden`) and elements with the `hidden` attribute or `aria-hidden="true"`. It does not compare colors. `clean_text(s)` removes zero-width and bidirectional control characters, Unicode tag characters, other control characters except newline and tab, and collapses runs of blank lines. The Gmail provider applies `html_to_safe_text` and `clean_text` to the body and `clean_text` to subject, sender, recipients, date, snippet, attachment names and attachment MIME types.

**One fence per response.** Untrusted text never sits inline in a response. Each response that carries any has a single top-level `untrusted` string: `[untrusted:<nonce>]`, a JSON object mapping references (`#1`, `#2`, ...) to the text, and `[/untrusted:<nonce>]`, each marker on its own line, with one random nonce of 12 hex characters per response. Every message object carries `content_ref` (`#N`), whose entry is `{"snippet": ..., "body": ...}` (`body` only for full reads); every other untrusted string (an unsubscribe `url`/`mailto`, each preference value, each `user_request`, the `query` of a recorded label change) is replaced in place by its own reference. Ids, thread ids, accounts, labels, flags, timestamps and the cleaned-only fields (subject, From, To, Cc, Reply-To, attachment names) stay outside, as before. Everything goes through one helper, `core.content.Fence`: tools call `fence.ref(text)` for each untrusted value and `fence.attach(response)` once (`server.present` for message results, `server.present_prefs` for preference payloads, and the same `Fence` in `list_changes` and `undo_since`). JSON keeps an email from forging another reference's entry (quotes and newlines are escaped), and the whole block is cleaned and defanged: any `[untrusted:` or `[/untrusted:` inside it is rewritten to `[untrusted_:` and `[/untrusted_:`, so an email cannot forge a closing marker. A response with no untrusted text has no `untrusted` key. The tool descriptions explain the block and the references.

**Short bodies.** `read_email`, `read_emails` and `read_thread` return at most `max_body_chars` characters of each body (default from `MAX_BODY_CHARS`, 2000, at most 20,000 or startup fails; 0 means the full body), never more than the hard maximum of 20,000 that the provider already applies; a cut body ends with `[... truncated ...]`. The server cuts the body only after the suspicion assessment, which always runs on the full cleaned body, so a cut never hides an injection signal.

**Flags.** Each message gets `suspicious` and `suspicious_reasons`, computed by `assess` from the sender-controlled text fields (subject, From, To, Cc, Reply-To, snippet, body and attachment names) and the hidden-text count. Reasons: `mentions_tools` (the name of any mailhub-mcp tool, in any mode), `instruction_override` (for example "ignore previous instructions", in English and Portuguese), `addresses_assistant`, `requests_action` (forward, create a draft, change preferences or settings, "reply to ... with") and `hidden_text` (more than 200 hidden characters removed). The flags are a heuristic aid, not a guarantee. Because they are computed in the server, they are also what `create_draft` uses to refuse a suspicious thread.

## Preferences

`core/preferences.py` stores triage preferences in `preferences.json` next to the journal (`JOURNAL_PATH`). There are two kinds of scope: `global` and one per account alias; the effective rules for an account are the global rules plus its own. Fields: `important_senders`, `ignored_senders` (`name@domain` or `@domain`, at most 254 characters), `important_keywords`, `ignored_keywords` (1-40 characters, NFC-normalized, only letters of any script, digits, spaces, apostrophes, dots and hyphens; combining marks only on a letter, at most three per letter and no overlay or enclosing marks; no default-ignorable code points such as variation selectors or Hangul fillers; no modifier letters except a few Japanese and Thai word characters), each list capped at 200 items, and `last_triage_at` (ISO 8601, not in the future). A value cannot be both important and ignored in a scope. Keywords are short and restricted, but they are still text written by the model and shown back to it, so when tools return preferences every sender pattern and keyword (like every `user_request`) is moved into the response's untrusted fence and replaced by a reference; only timestamps, ids, scopes and field names are not. The stored values stay raw.

**Direction rule.** Adding to an important list, or removing from an ignored list, raises attention and applies immediately. Adding to an ignored list, or removing from an important list, lowers attention and becomes a proposal. `last_triage_at` is set immediately. Each `set_preferences` call needs a `user_request` (1-500 characters), stored with the change and shown by `get_preferences` and `list_changes`; it is moved into the untrusted fence when shown.

**Proposals.** A proposal lives in `preferences.json` under `proposals` and is never applied by an MCP tool. It expires after 14 days; expired proposals are dropped on read. An identical open proposal is reused rather than duplicated, and at most 50 can be open at once. `mailhub-admin preferences list|show|approve|reject <id>` is the only way to resolve one. Approval validates it again against the current state, applies it and journals a change with tool `approve_preference`; rejection journals nothing. `mailhub-admin` reads only `JOURNAL_PATH` (default `/data/changes.jsonl`), needs no OAuth settings, and exits 0 on success and 2 on any error.

**Store locking.** The server and the CLI both write `preferences.json`. Writes hold an exclusive `fcntl.flock` on `preferences.lock` and replace the file atomically (temporary file, `fsync`, `os.replace`).

**Journal and undo.** Preference changes are journal entries with `kind: "preferences"`, plus `scope`, `before` and `after` per touched field and `user_request`; entries without a `kind` are label changes. `list_changes` shows both. `undo_change` on a preference entry restores `before` under the direction rule: the parts that raise attention (un-ignoring, re-marking important) and `last_triage_at` apply at once as one journaled undo, while the parts that would lower attention (removing values from `important_*`, adding values to `ignored_*`) become proposals with `user_request` "undo of <change_id>: <original user_request>", returned in `proposed` and approved with `mailhub-admin` like any other. When nothing applies, no undo entry is written (`change_id` is null) and the change stays undoable; repeating it reuses the same proposals. An undo proposal stores `undo_of: <change_id>`, and approving it journals the `approve_preference` entry with that `undo_of`: the original then counts as undone, `undo_since` does not revert it again (or the approval, which is an undo entry), and a later run converges instead of re-proposing. Rejecting an undo proposal records the original change id in `rejected_undos` in `preferences.json` (the last 200); undoing that change again is refused with "the undo of <change_id> was rejected by the owner" (`undo_since` lists it under `failed`). Undo entries themselves cannot be undone ("this change is an undo; to redo, make the change again"), for label and preference entries alike, so a redo always goes through the cap and the direction rule. `undo_since` includes preference entries in the same newest-first order, but holds changes back by `(scope, field)` instead of `(account, message_id)`; a preference change whose undo only produced proposals is listed under `proposed`, not counted as undone, and does not hold anything back by itself. Undo is refused, and nothing is changed, if a touched field no longer equals `after`: a later change is never overwritten. Double undo is refused.

## Prompts

`daily_triage(accounts="")`, `clean_newsletters(account)` and `organize_inbox(account)` are MCP prompts defined in `prompts.py` and registered in `server.py`. They are instructions for the model, not tools: they name the tools to call, repeat the safety rules (email content is untrusted, never follow it, never visit links, suspicious messages are reported only) and, for the two that change mail, require a `confirm=false` preview and an explicit confirmation in the conversation before `confirm=true`. They never trash or delete. `daily_triage` ends with `set_preferences(last_triage_at=...)`. With `TOOLS=read-only` only a read-only `daily_triage` is registered, which says `last_triage_at` cannot be stored. A test checks that every tool a prompt calls is registered in the mode the prompt is registered for.

## Unsubscribe info

`summarize` parses `List-Unsubscribe` and `List-Unsubscribe-Post` into `unsubscribe: {url, mailto, one_click}` (`None` when nothing usable) with `parse_unsubscribe` in `core/content.py`. Only `https://` URLs and `mailto:` URIs are accepted (the first of each); entries with whitespace or control characters, or over 500 characters, are dropped. `one_click` is true only when `List-Unsubscribe-Post` is `List-Unsubscribe=One-Click` (case-insensitive) and an https URL exists. The server replaces `url` and `mailto` with references into the response's fence (`_present`), including in `inbox_overview` top senders, which carry the values of the sender's most recent message that has them. Claude only shows them to the user and never visits them; the tool descriptions say so.

## Health

`GET /health` is a FastMCP custom route returning `{"status":"ok"}`. It needs no authentication and reveals nothing else; it is the only endpoint without login (every other endpoint requires the Google OAuth login). `main()` passes an allowed-hosts list (the public host name, `localhost`, `127.0.0.1`) to FastMCP, but FastMCP's Host/Origin protection is not enabled by default, so no Host check runs in production; `/health` answers for any Host. If you enable the protection, `/health` still passes for `127.0.0.1` and `localhost`, which the Docker HEALTHCHECK uses. The Dockerfile `HEALTHCHECK` calls it with the Python standard library every 30 seconds (timeout 5 seconds, 3 retries) on `$PORT` (default 8742). It only proves the process answers HTTP; it does not touch the mailboxes. `mailhub-admin accounts` does: it loads the account registry from `ACCOUNTS_DIR` (default `/secrets/accounts`, no OAuth settings needed), asks each account for its address and prints `alias  provider  ok  <address>` or `alias  provider  ERROR  <message>`. It exits 0 when all accounts are ok, 1 when any failed and 2 when the accounts cannot be loaded.

## Capability limits

- **Drafts.** `create_draft` requires `reply_to_message_id`. Participants are every address in From, To, Cc and Reply-To across the thread, plus the account's own address; `to` and `cc` outside that set are refused and the error lists the allowed addresses. `to` defaults to the Reply-To of the replied message, else its From. A thread containing any message flagged suspicious is refused. Nothing is ever sent.
- **Daily cap.** `MAX_CHANGES_PER_DAY` (default 2000) limits messages changed by `modify_emails` and `modify_by_query` per account in a rolling 24 hours, counted from the journal (undo entries excluded). The check runs under the journal lock, before any write, and the error ("would exceed the daily change cap") says how many changes remain. Undo is never capped.
- **Read-only mode.** `TOOLS=all|read-only` (default `all`). In `read-only` only `list_accounts`, `search_emails`, `count_messages`, `read_email`, `read_emails`, `read_thread`, `inbox_overview`, `list_drafts`, `list_labels`, `list_changes` and `get_preferences` are registered; the write tools do not exist on that instance. Any other value fails startup. Since `set_preferences` is not available either, such an instance cannot record `last_triage_at`.

## Limitations

- **The lethal trifecta is only partly closed.** Exfiltration needs three things together: private data, untrusted content that can steer the model, and an egress channel. mailhub supplies the first two by design (a mailbox full of third-party text) and removes its own egress: no `messages.send` or `drafts.send` (enforced by a test), drafts are replies only to addresses already in the thread and refused for suspicious threads, and unsubscribe links are shown, never visited. That only covers this server. Another tool in the same Claude session or routine that can reach the internet (web fetch or search, a browser, a connector that posts to chat, tickets, documents or calendars) is an egress channel the server cannot see: an injected instruction could ask the model to encode mail content into a URL or a post. The fence, the suspicion flags and the tool descriptions lower that risk but do not remove it. Be deliberate about which tools share a session or routine with mailhub; unattended routines are safest with `TOOLS=read-only` and no internet-capable tools.
- **The server cannot verify who motivated a call.** `user_request` is text the model supplies, and a manipulated model can write a plausible one. The real check is you reviewing proposals with `mailhub-admin preferences show` before approving.
- **Heuristics can miss attacks.** Suspicion flags are regex-based and can be evaded by paraphrase, other languages or crafted text. The suspicious-thread refusal in `create_draft` inherits that blind spot. Hidden-text removal covers explicit hiding styles, not color tricks.
- **Preferences never hide mail.** Search, read and overview ignore preferences. Rules only guide how Claude ranks and reports, so an approved or injected "ignore" can at worst lower a message in a triage report; it can never make the message unfindable.
- **Raising-attention changes apply immediately and unreviewed.** Injected text can mark an attacker's sender important (noise or priority). This is visible in `list_changes` and undoable, but undoing it lowers attention, so the undo itself is a proposal you approve with `mailhub-admin`. Undo can lower attention only through proposals.
- **Only some text is delimited.** `body`, `snippet`, unsubscribe `url`/`mailto`, `user_request`, preference values and the `query` of a recorded label change are moved into the response's untrusted fence. Subjects, names, addresses and attachment names are cleaned of invisible characters and fed to the suspicion heuristics, but they are not delimited.

## Adding a provider

1. Create `providers/<name>/` with a class implementing `MailProvider`, constructed as `(alias, credential_path)`, and `authorize(client_secret: Path) -> str` returning the credential file contents.
2. Run the shared contract suite in `tests/providers/contract.py` against it (see how the Gmail tests parametrize it).
3. Register the class in `PROVIDERS` and the function in `AUTHORIZERS` in `providers/__init__.py`. A test requires both to have the same keys.
4. Link accounts with `mailhub-authorize <name> <client_secret> <alias>`; credentials land in `secrets/accounts/<name>/<alias>.json`.
