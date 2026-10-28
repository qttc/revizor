# Security

Revizor runs a local HTTP server on `127.0.0.1` while a document is open for review.

## Threat model

- The server serves the document folder only. Hidden files and folders (`.git`, `.env`) and review files (`*.review.json`) are never served.
- Requests with a foreign `Host` header are rejected, which blocks DNS rebinding. `POST` requests need `Content-Type: application/json` and a same-origin `Origin`, so other sites in the browser cannot change or finish a review.
- Opening an HTML document in Revizor means trusting it: its scripts run on the same origin as the editor and can call the editor API. Open only documents you would open in a browser anyway.
- Revizor makes one network call a day: `git ls-remote` to check for a new release tag. Set `REVIZOR_NO_UPDATE_CHECK=1` to turn it off. Documents and edits never leave the machine.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/qttc/revizor/security/advisories/new). Do not open a public issue. You will get a reply within a week.

## Supported versions

Only the latest release gets security fixes.
