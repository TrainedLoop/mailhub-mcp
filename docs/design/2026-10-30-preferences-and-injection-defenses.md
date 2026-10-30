# Triage preferences and prompt-injection defenses

Status: proposed
Date: 2026-10-30

## Context

mailhub-mcp lets Claude read and organize mailboxes. Every email it reads is text written by a third party, and Claude may treat instructions hidden in that text as if the user had given them (indirect prompt injection). The existing defenses limit the damage:

- nothing can be sent, trashed or deleted;
- protected labels are refused;
- every change is journaled and undoable (`undo_change`, `undo_since`);
- changed messages carry `claude/organized`.

This design adds **triage preferences**: remembered rules such as "this sender is always important". They make scheduled triage consistent from one run to the next. Preferences are also the first state that **persists** between sessions. A successful injection could therefore keep working silently, for example by hiding a bank's fraud alerts from every future report. The design also hardens the content Claude receives and narrows what a manipulated Claude can do.

### Threat model

The attacker can send email to any connected mailbox and can control the visible and hidden text, the subject, the sender name and attachment names. The attacker cannot call the MCP server directly, which requires Google login with an allowlisted email.

The server cannot tell whether a tool call was motivated by the user or by injected text: both arrive as the same call from Claude. The defenses therefore aim to make every injected action either **visible and reversible** or **inert until a human approves it outside the chat**.

### Goals

- Preferences that raise attention apply immediately. Preferences that lower attention never apply without out-of-band approval.
- Every preference change is journaled with the user request that motivated it, listed by `list_changes`, and undoable.
- Hidden text and invisible characters are removed before Claude sees an email. Email-derived text is clearly delimited, and likely injection attempts are flagged.
- Drafts cannot be addressed to people outside the thread being answered.
- The number of messages changed per account per day is capped.
- Operators can run a read-only instance.

### Non-goals

- Proving who motivated a tool call. This is impossible at the server; see the threat model.
- A web approval page. Approval is a CLI command for now; a page can come later.
- Server-side filtering by preferences. Preferences are data that Claude uses for triage. They never hide mail from any tool.
- MCP prompts for triage. That is a separate feature.

## Preferences

### Data model

Stored in `<data>/preferences.json`. Scopes are `global` and one entry per account alias. The effective rules for an account are the global rules plus that account's rules.

| Field | Type | Add | Remove |
|---|---|---|---|
| `important_senders` | list of sender patterns | immediate | proposal |
| `important_keywords` | list of keywords | immediate | proposal |
| `ignored_senders` | list of sender patterns | proposal | immediate |
| `ignored_keywords` | list of keywords | proposal | immediate |
| `last_triage_at` | ISO 8601 UTC timestamp | immediate (set) | — |

The rule is direction: anything that raises attention, or removes a reduction of attention, is immediate. Anything that lowers attention becomes a proposal.

Validation:
- A sender pattern is an email address (`name@domain`) or `@domain`, lowercased, at most 254 characters.
- A keyword has 1–40 characters, with no control characters or newlines.
- Each list holds at most 200 items.
- A value cannot be both important and ignored in the same scope; such a request is refused.
- Unknown fields, unknown scopes and invalid values are refused, with a message naming them.
- No free-text field exists, so stored preferences cannot carry instructions back into Claude's context.

### Tools

`get_preferences(account: str | None = None)`, read-only. It returns:
- `global`, and either the per-account rules (`accounts`) or, when `account` is given, that account's rules plus the `effective` merge;
- `pending`: open proposals, with id, scope, field, values, `user_request`, created timestamp and expiry;
- `recent_changes`: preference changes since the effective `last_triage_at`, or the last 20 when it is unset. Each has change_id, timestamp, applied/approved/rejected, field, values and `user_request`.

`set_preferences(scope: str, add: dict[str, list[str]] | None = None, remove: dict[str, list[str]] | None = None, last_triage_at: str | None = None, user_request: str)`, a write tool.
- `user_request` is required, 1–500 characters: the user's words that asked for the change. It is stored and shown in `get_preferences` and `list_changes`.
- The description says to call the tool only when the user explicitly asked in the current conversation, and never because of email content.
- Each requested operation is classified by the direction rule.
- Immediate operations are applied atomically and journaled as one change (`tool: "set_preferences"`). Proposal operations become one proposal each.
- Returns `{"change_id": str | None, "applied": [...], "pending": [{"proposal_id", "scope", "field", "values"}...]}`.
- A request where every value is already in the target state returns nothing applied and nothing pending.

### Proposals and approval

- A proposal is stored in `preferences.json` under `proposals`.
- Proposals expire after 14 days. Expired proposals are dropped on read and never applied.
- Approval happens outside the chat with a new console command, run inside the container:
  - `mailhub-admin preferences list`
  - `mailhub-admin preferences show <id>`
  - `mailhub-admin preferences approve <id>`
  - `mailhub-admin preferences reject <id>`

  Approving validates the proposal again against the current state, applies it and journals a change with `tool: "approve_preference"` and the proposal's `user_request`. Rejecting journals nothing and removes the proposal. Both print the result.
- No MCP tool can approve or reject a proposal.
- The server and the CLI both write `preferences.json`. Writes take an exclusive `fcntl.flock` on `<data>/preferences.lock` and replace the file atomically (write to a temp file, `fsync`, `os.replace`).

### Journal, history and undo

Preference changes use the existing journal with an entry kind:
- `kind: "preferences"`;
- `scope`;
- `before` and `after` for each touched field, as lists, or the timestamp for `last_triage_at`;
- `user_request`.

Label changes keep their current shape (`kind` absent means `"labels"`).

- `list_changes` shows preference entries with `tool`, `scope`, `fields`, `user_request` and `undone`.
- `undo_change` on a preference entry restores `before` for the touched fields. This restores the user's earlier state, so it is allowed even when it lowers attention. Undo is journaled like other undos, and double undo is refused.
- `undo_since` includes preference entries in the same newest-first order. Blocking for preference entries is keyed by `(scope, field)` instead of `(account, message_id)`.
- Undo is refused with a clear message if the current value no longer equals `after` for a touched field, meaning something changed it since. Undo never overwrites a later change.

## Content defenses

These apply to every message returned by any tool: search, read, thread, drafts and overview.

### Sanitizing

A provider-agnostic `core/content.py` provides:
- `html_to_safe_text(html) -> (text, hidden_removed: int)`. It drops these and their contents:
  - `<head>`, `<script>`, `<style>`, `<template>`, `<noscript>`;
  - comments;
  - elements whose inline style hides them: `display:none`, `visibility:hidden`, `opacity:0`, a `font-size` of `0`/`1px` or below, `max-height:0` with `overflow:hidden`;
  - elements with the `hidden` attribute or `aria-hidden="true"`.

  It keeps the visible text. It does not try to compare colors.
- `clean_text(s) -> str`. It removes zero-width and bidirectional control characters (U+200B–U+200F, U+202A–U+202E, U+2060–U+2064, U+2066–U+2069, U+FEFF, plus U+00AD, U+061C, U+180E and the Unicode tag characters U+E0000–U+E007F) and other C0/C1 control characters except `\n` and `\t`, and collapses runs of blank lines.

The Gmail provider uses these for the body, and `clean_text` for the subject, sender, recipients, snippet and attachment names.

### Delimiting

Every tool response that contains email text uses one random nonce per response, 12 hex characters. `body` and `snippet` values are wrapped as `[untrusted:<nonce>]\n…\n[/untrusted:<nonce>]`. Any occurrence of `[untrusted:` or `[/untrusted:` inside the content is defanged first by rewriting it to `[untrusted_:` or `[/untrusted_:`. The read tools' descriptions explain the markers and that nothing inside them is an instruction.

### Suspicion flags

Each message gains `suspicious: bool` and `suspicious_reasons: list[str]`, computed by `core/content.py` `assess(text_fields, hidden_removed)`. The reasons:
- `mentions_tools`: the name of any mailhub-mcp tool;
- `instruction_override`: case-insensitive patterns in English and Portuguese such as "ignore (all|previous|prior) instructions", "disregard", "you are (now )?(an? )?(ai|assistant|claude)", "ignore as instruções", "você é (um|o) assistente";
- `addresses_assistant`: phrases such as "assistant:", "ai assistant", "claude,";
- `requests_action`: asks to forward, create a draft, change preferences or settings, or "reply to … with";
- `hidden_text`: `hidden_removed` above a small threshold, 200 characters.

The flags are a heuristic aid, documented as such. The triage guidance tells Claude to report flagged emails and not to act on instructions in them.

## Capability limits

### Drafts: replies only

- `create_draft` requires `reply_to_message_id`. New threads are refused with a clear message.
- The participants of the thread are every address in From, To, Cc and Reply-To across its messages. The account's own address is always allowed.
- `to` and `cc` must contain only participants. Any other address is refused, and the error lists the allowed addresses.
- When `to` is omitted, it defaults to the Reply-To of the replied message, or its From.
- The tool description and docs state the policy.

### Daily change cap

`MAX_CHANGES_PER_DAY` (default 2000) caps the number of messages changed by `modify_emails` and `modify_by_query` per account in a rolling 24 hours. The count comes from journal entries (`messages` count of label entries, excluding undos). A request that would exceed the cap is refused before any write, and the error says how many changes are left. Undo is never capped.

### Read-only mode

`TOOLS=all|read-only` (default `all`). In `read-only`, the server registers only the read-only tools:
- `list_accounts`, `search_emails`, `read_email`, `read_emails`, `read_thread`, `inbox_overview`;
- `list_drafts`, `list_labels`, `list_changes`, `get_preferences`.

The write tools are not registered at all. An invalid value fails startup with a clear message.

## Configuration

New environment variables: `MAX_CHANGES_PER_DAY` and `TOOLS`. `preferences.json` and `preferences.lock` live in the directory of `JOURNAL_PATH`.

## Testing

- **Preferences:**
  - validation of every field and limit;
  - the direction rule for each field and operation;
  - the important/ignored conflict;
  - atomic apply and journaling with `user_request`;
  - proposals created, expiring after 14 days, and never applied by any MCP tool;
  - CLI list, show, approve (re-validated, journaled) and reject;
  - concurrent server and CLI writes serialized by the file lock;
  - `undo_change` and `undo_since` on preference entries, including refusal when the value changed since;
  - `get_preferences` output, including `effective` and `recent_changes`.
- **Content:**
  - each hiding technique removed;
  - visible text kept;
  - invisible and bidi characters removed from every text field;
  - the delimiters, the nonce changing per response, and marker defanging;
  - each suspicion reason, plus a benign newsletter not flagged.
- **Drafts:** a reply to a participant is allowed; an outsider is refused, with the allowed list; a new thread is refused; the default recipient.
- **Cap:** a request over the cap is refused with no writes; the rolling window; undo not counted.
- **Read-only mode:** only the listed tools are registered; invalid `TOOLS` fails startup.
- **Unchanged guarantees:** the boundary and forbidden-call tests stay green.

## Documentation

- `README.md`: preferences and the safety model.
- `docs/tutorial.md`:
  - approving proposals with `mailhub-admin`;
  - a "scheduled triage" section describing the recommended routine flow: get_preferences, search since `last_triage_at`, report needs-attention, FYI, suspicious emails, preference changes and pending proposals, then set `last_triage_at`;
  - `TOOLS=read-only` for routines that should only report.
- `docs/architecture.md`: content defenses, preferences and the journal entry kinds.
