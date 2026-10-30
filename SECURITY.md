# Security policy

mailhub-mcp gives an AI assistant access to a person's mailboxes, so security reports are taken seriously.
This is a personal open-source project: responses are best effort, with no guaranteed timeline.

## Supported version

Only the latest code on `main` is supported. Please reproduce the problem there before reporting.

## Reporting a vulnerability

Report privately. Do not open a public issue or pull request for a vulnerability.

Use the repository's **Security** tab, then **Report a vulnerability** (GitHub private vulnerability
reporting / Security Advisories).

## In scope

- Prompt-injection bypasses: email content that makes the assistant act against the owner's wishes
- Sanitizer or untrusted-marker bypasses (hidden text, look-alike characters, marker forgery)
- Authentication and allowlist flaws (`ALLOWED_EMAILS`, token handling, OAuth)
- Bypasses of the safety model: sending, trashing or deleting mail, protected labels, journal and undo, preference approval
- Exposure of mail content, tokens or credentials to anyone other than the owner

## Out of scope

- Problems that require access to the host, the `.env` file or the credentials folder
- Issues in third-party services or dependencies without a way to exploit them here
- Running the server without HTTPS or with an allowlist that includes other people

## What to include

- What you found and its impact
- Steps or a minimal proof of concept (a failing test is ideal)
- The commit you tested against and your configuration (`TOOLS`, number of accounts)

Please do not include real mailbox content or credentials in a report.

## What to expect

I will acknowledge the report when I can, assess it, fix it on `main` and credit you if you wish.
Please allow reasonable time for a fix before disclosing publicly.
