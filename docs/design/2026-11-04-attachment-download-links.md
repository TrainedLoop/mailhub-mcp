# Attachment download links

Status: proposed
Date: 2026-11-04

## Context

`read_email` lists each attachment's file name, type and size, but the owner has no way to get the file from the conversation: they have to open Gmail, find the message and download it there. This design adds a download link to every listed attachment. The owner clicks it, signs in to `/admin` if needed, and the server streams the file from the mailbox to the browser.

### Goals

- Every attachment listed by a read tool carries either a download link or the reason it has none.
- Only the owner can download: a link alone gives access to nothing.
- A link keeps working for 14 days, long enough for a summary read days later.
- The model never sees attachment content, and no MCP tool can fetch it.
- Nothing is written to disk; no new configuration and no new state.

### Non-goals

- The model reading attachment content (text of a PDF invoice, for example). That stays in the roadmap as its own feature, with its own defenses.
- Revoking one link. All links end together when `JWT_SIGNING_KEY` rotates.
- Inspecting archive contents. Gmail already refuses archives that hold executables at delivery.

### Threat model

Download links end up in conversation history, in scheduled routine output and possibly in notifications, and the server is reachable from the internet. The design defends against:

- someone who sees a link downloading the file (the endpoint requires the owner's `/admin` session);
- a forged or edited link reaching another message, attachment or mailbox (HMAC-signed token);
- an old link working forever (14-day expiry);
- an attachment executing in the server's origin, such as an HTML or SVG file opened inline (forced download, generic content type, `nosniff`, restrictive CSP);
- the easy path to a likely-malicious file: attachments of spam messages, of messages flagged `suspicious`, and of file types Gmail blocks get no link, and the rules are checked again at download time;
- email text steering the model into fetching content: links are inert strings for the model, and only a browser with the owner's session can follow them.

## Design

### Identifying an attachment

Gmail's `attachmentId` changes on every read of a message, so it cannot go in a link. The MIME part id (`partId`, such as `"2"` or `"1.3"`) is stable within a message and is used instead.

- `Attachment` gains `part_id`, filled by the Gmail parser.
- `Message` gains `spam: bool`, set by the provider (for Gmail, the `SPAM` label). Core does not know provider label names.

### Provider interface

`MailProvider` gains `get_attachment(message_id: str, part_id: str) -> bytes`. The Gmail provider fetches the message, finds the part, and either decodes its inline `body.data` (small attachments) or calls `messages.attachments.get` with the part's current `attachmentId`. An unknown message or part raises the existing not-found error. The core/provider boundary test keeps covering the new code.

### Blocking rules

One core function, `download_block_reason(message, attachment, tool_names) -> str | None`, returns:

- `spam` when the message is in spam;
- `suspicious` when the message's suspicion assessment returns any reason;
- `blocked_type` when the file name's extension is on the list of types Gmail blocks (`.ade .adp .apk .appx .appxbundle .bat .cab .chm .cmd .com .cpl .diagcab .diagcfg .diagpack .dll .dmg .ex .ex_ .exe .hta .ins .isp .iso .jar .jnlp .js .jse .lib .lnk .mde .mjs .msc .msi .msix .msixbundle .msp .mst .nsh .pif .ps1 .scr .sct .shb .sys .vb .vbe .vbs .vhd .vxd .wsc .wsf .wsh .xll`), compared case-insensitively on the last extension;
- `None` otherwise.

The suspicion assessment moves from `server.py` into core so that the read tools and the download endpoint share it. It still takes the server's tool names, which the admin context receives when it is built.

### Links in read results

Wherever the server presents a full message (the `_present` step used by `read_email`, `read_emails` and `get_thread`), each attachment gains either:

- `download_url`: `<PUBLIC_BASE_URL>/admin/files/<token>`, or
- `download_blocked`: one of `spam`, `suspicious`, `blocked_type`.

With `ADMIN=off` neither field is added. Creating a link is a signature only: it costs no Gmail quota and writes nothing. The read tools' descriptions say that a link requires the owner's login, expires after 14 days, and should be passed to the user unchanged.

### Token

The token is a payload `{"a": alias, "m": message_id, "p": part_id, "exp": unix_time}` signed with the existing `Signer` (HMAC-SHA256), under a key derived from `JWT_SIGNING_KEY` with HKDF and the label `mailhub-attachment-link`. The label differs from the session cookie's, so a link token can never pass as a session or flow cookie and the other way round. Expiry is 14 days after the read, a constant like the other admin durations.

### Download endpoint

`GET /admin/files/<token>`, registered with the other admin routes and only when `ADMIN` is on:

1. Verify the token's signature and expiry. On failure: a short page, "this link has expired or is invalid; ask for the message to be read again", status 410 for expired and 404 for invalid.
2. Require the owner session.
   - With no valid session, go through the Google login and return to the same path. The return path is checked by the existing `safe_path`.
   - The session cookie is `SameSite=Strict`, so a click from claude.ai arrives without it. When the request has no session cookie and was not already bounced, the server answers with the same-site refresh page used after the OAuth callbacks, pointing to the same URL with a `bounced=1` marker. Only after that second, same-site request finds no session does it start the login.
   - Downloading does not require the 5-minute recent re-authentication; that rule stays for link, re-link and remove.
3. Look up the alias in the account registry. A removed alias gives 404, "this mailbox is no longer linked".
4. Fetch the message in full and find the part. A missing message or part gives 404, "this message or attachment no longer exists".
5. Apply `download_block_reason` again. A message that moved to spam or is now suspicious is refused with 403 and the reason, plus a note that the file can still be downloaded in Gmail.
6. Call `get_attachment` and send the bytes.

Network calls run through the admin `CapacityLimiter`. The file is held in memory only while it is sent; Gmail limits attachments to 25 MB.

The response carries:

- `Content-Type: application/octet-stream`, never the type declared in the email;
- `Content-Disposition: attachment; filename="<ascii fallback>"; filename*=UTF-8''<percent-encoded name>`, where the name has control characters, path separators, quotes and leading dots removed and is cut to 150 characters, with `attachment` as the fallback when nothing is left;
- `X-Content-Type-Options: nosniff`;
- the `/admin` security headers (CSP `default-src 'none'`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`).

Each download costs about 25 Gmail quota units (a full message read and an attachment read), counted against the account's budget like any other call.

### Audit log

Granted and refused downloads are logged with the owner email, alias, message id, file name, size and refusal reason. Tokens are never logged; the uvicorn access log is already off, which matters because the token is in the path.

### Configuration

No new variables. Links exist when `ADMIN=on`, since they depend on the `/admin` login. The link lifetime (14 days) is a constant.

## Testing

- **Unit.**
  - Token: signing, tampering, expiry at 14 days, and key separation from the session key.
  - `download_block_reason`: spam, suspicious, every blocked extension, case and double extensions (`invoice.pdf.exe`), and a clean attachment.
  - File name sanitizing: Unicode, quotes, line breaks, `../`, an empty result, a very long name.
- **Gmail provider** against a fake Google: inline attachment, attachment fetched by `attachmentId`, unknown part, `part_id` and `spam` filled by the parser.
- **Read tools.** Attachments carry `download_url` or `download_blocked`; with `ADMIN=off` neither appears; reading a message makes no extra Gmail call.
- **Routes** with Starlette's test client:
  - without a session cookie, the first request gets the refresh page and the bounced one redirects to login, which returns to the download;
  - an expired token gives 410 and a tampered one 404;
  - a message that became spam or suspicious after the link was made is refused;
  - a removed alias, message or part gives 404;
  - a granted download has the headers above and the exact bytes;
  - `ADMIN=off` gives 404.
- **Boundaries.** No MCP tool module calls `get_attachment`; the core/provider boundary test still passes.
- **Manual, on a real host.** Download a real PDF by clicking the link in claude.ai, on desktop and in the mobile app, with and without an active session.

## Documentation

- README: attachment links under features and the safety model.
- Tutorial: a short note on how links work and why they ask for login.
- Roadmap: rename the "Safe attachment download" idea to make clear it is about the model reading attachment content.
