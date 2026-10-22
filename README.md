# gmail-mcp — multi-account, multi-provider mail MCP server

An [MCP](https://modelcontextprotocol.io) mail server that connects **multiple
accounts across providers** — **Google (Gmail)** and **Microsoft (Outlook/
Hotmail)** — behind one set of mail tools, plus native provider settings tools.
Read tools take an optional `account` (email); omit it to use the default.
Applying filter/rule changes requires an explicit account. `search_all_threads` fans out
across **all** connected accounts in one call, mixing Gmail and Outlook.

Architecture in one line: an **encrypted token store keyed by email, with provider collision guards** routes
each call to the right account, and a small **provider abstraction**
(`providers.py`) dispatches Google calls to the Gmail API and Microsoft calls to
Microsoft Graph — so the tools never fork per provider.

## Layout

```
gmail-mcp/
├── gmail_mcp/
│   ├── config.py            # static config (Google scopes; Microsoft client id, authority, scopes)
│   ├── store.py             # SQLite store keyed by email; provider + one Fernet-encrypted auth_data blob
│   ├── auth.py              # Google OAuth + Microsoft MSAL device-code; per-provider credential refresh
│   ├── providers.py         # MailProvider abstraction: provider_for() -> GmailProvider | GraphProvider
│   ├── gmail.py             # Gmail API response helpers
│   ├── graph.py             # Microsoft Graph mail helpers (stdlib urllib)
│   ├── rules.py             # native Gmail filters / Outlook Inbox rules, preview and recovery
│   ├── drive.py             # Google Drive helpers (Google accounts only)
│   ├── server.py            # FastMCP server; all tools
│   ├── connect_microsoft.py # `python -m gmail_mcp.connect_microsoft [key-email]`
│   └── __main__.py          # `python -m gmail_mcp`
├── credentials/             # client_secret.json (you add) + accounts.db + fernet.key (runtime). Gitignored.
└── requirements.txt
```

## One-time setup

### Google (Gmail / Drive)

1. [console.cloud.google.com](https://console.cloud.google.com) → enable the **Gmail API** (and Drive API).
2. **OAuth consent screen** → External → add yourself as a **Test user**.
3. **Credentials → OAuth client ID → Desktop app** → download JSON → save as `credentials/client_secret.json`.

The **Desktop app** type enables the loopback redirect `add_account` uses.

### Microsoft (Outlook / Hotmail)

Uses an Entra **public client** app (device-code flow, no secret). The app id is
already set in `config.py` (`MS_CLIENT_ID`). If you ever recreate it: register an
app with **"Any Entra ID tenant + personal Microsoft accounts"**, enable
**Allow public client flows**, and under **Authentication → Mobile & desktop**
add the redirect URIs `https://login.microsoftonline.com/common/oauth2/nativeclient`
**and** `https://login.live.com/oauth20_desktop.srf` (the second is required for
personal accounts, or device login errors with `redirect_uri`).

### Python env

```bash
cd ~/Developer/gmail-mcp
python3.13 -m venv .venv
.venv/bin/python -m pip install .
```

Python 3.13 or newer is required. `requirements.lock.txt` supplies pinned runtime
dependencies; `requirements-dev.txt` adds pinned lint/build tools for verification.

## Connecting accounts

**Google** — opens a browser for consent:
```bash
.venv/bin/python -c "from gmail_mcp import auth; print(auth.add_google_account())"
```

**Microsoft** — device-code login (prints a URL + code to enter):
```bash
.venv/bin/python -m gmail_mcp.connect_microsoft
```
> If the Microsoft account's **primary alias equals an existing Google
> account's email**, pass the intended address so it stores separately and
> doesn't collide, e.g. `python -m gmail_mcp.connect_microsoft you@outlook.com`.

The **first** account connected becomes the default.

## Wiring into an MCP client

stdio server:

```json
{
  "mcpServers": {
    "gmail-multi": {
      "command": "/Users/ronildo/Developer/gmail-mcp/.venv/bin/python",
      "args": ["-m", "gmail_mcp"],
      "cwd": "/Users/ronildo/Developer/gmail-mcp"
    }
  }
}
```

## Tools

| Tool | Google | Microsoft |
|------|:------:|:---------:|
| `add_account` (Google) / `connect_microsoft` (CLI) | ✅ | ✅ |
| `list_accounts` / `set_default_account` / `remove_account` | ✅ | ✅ |
| `get_account_capabilities` (no token refresh) | ✅ | ✅ |
| `search_threads` / `get_thread` | ✅ | ✅ |
| `search_all_threads` (cross-account, cross-provider) | ✅ | ✅ |
| `create_draft` / `list_drafts` / `send_message` | ✅ | ✅ |
| `archive_thread` / `move_thread` / `list_folders` | ✅ | ✅ |
| `list_labels` / `create_label` / `label_thread` / `unlabel_thread` | ✅ | ✖ (not yet) |
| `drive_*` (list/search/read/create/move/rename/trash) | ✅ | ✖ (Google only) |
| `list_filters` / `get_filter` / `create_filter` / `delete_filter` / `replace_filter` | ✅ | ✖ |
| `list_inbox_rules` / `get_inbox_rule` / `create_inbox_rule` / `update_inbox_rule` / `delete_inbox_rule` | ✖ | ✅ |

`archive_thread` / `move_thread` unify filing across providers: Gmail moves
labels (remove `INBOX`; a folder name maps to a label), Outlook moves the
conversation between mail folders (well-known `Archive` / `Junk Email` /
`Deleted Items`, or a custom folder created on demand). Prefer a custom folder
over Outlook's built-in `Junk Email` / `Deleted Items`, which auto-purge.

Search syntax: Gmail uses Gmail query syntax (`is:unread from:a@b.com`);
Microsoft treats the query as free-text `$search`.

Search, draft lists and Drive lists/searches honor `max_results`, fill short API
pages, and return `next_page_token` (`null` when finished). Pass it back as
`page_token` with the same account and query. Graph cursors may contain buffered
message summaries: treat them as mailbox data, not secrets or portable bookmarks.
`search_all_threads` returns `next_page_tokens` by account; continue each account
through `search_threads`. Graph conversations and folder enumeration follow every
page and detect loops. Moving a Graph conversation enumerates all message IDs
before writing; partial failures include moved, failed and unattempted IDs.

## Filter and Inbox rule changes

These tools accept native [Gmail filter criteria/action objects](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.filters)
or [Graph messageRule fields](https://learn.microsoft.com/en-us/graph/api/resources/messagerule?view=graph-rest-1.0),
without translating between providers. Unsupported account/tool combinations
fail before acquiring a token. `get_account_capabilities` describes implemented
features and stored scopes; it does not guarantee the mailbox granted permission.

Every settings mutation defaults to `dry_run=true`, makes zero API writes, and
returns an `apply` object with the explicit account. Delete, replace and update
also return `expected_version`, bound to the account, provider, target and current
object. To apply, pass the returned `apply` fields to the same tool. A changed
default cannot redirect the operation; a stale object/version is rejected.
Versions are optimistic checks, not a provider transaction or a signed approval.
For example, preview disabling a rule:

```json
{"rule_id": "RULE_ID", "patch": {"isEnabled": false}, "account": "you@outlook.com"}
```

Review the resulting `before`, `patch`, `recovery` and `apply` fields before using
the returned apply arguments. Save recovery bodies outside the chat if needed;
they are returned to the caller, not automatically journaled. Mutation tool
annotations conservatively mark settings changes as destructive even in preview.

Gmail has no filter update endpoint. `replace_filter` creates the new filter,
then deletes the old one. Both can briefly be active; replacement needs room
under Gmail's filter limit. A deletion failure reports both IDs and a partial
result; a lost create response leaves the old filter untouched and reports an
uncertain outcome. There is no delete-first fallback or automatic write retry.

Outlook updates PATCH only explicitly supplied writable fields. Omitted
`sequence`, `exceptions`, and `isEnabled` stay unchanged; explicit `false` and
empty predicate objects are retained. Graph rules marked `isReadOnly` reject
update/deletion. Creating a rule requires an explicit sequence; Graph can
recalculate other rules' order. Recovery recreates settings, potentially with new
IDs/order, and cannot undo mail that a rule already forwarded, moved or deleted.

Permission errors provide reconnect guidance. Existing stored scopes are reused;
these tools never launch consent or request additional grants. Any reconnect is
a separate user decision. No account connection is needed to run the test suite.

## Development checks

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check gmail_mcp tests
.venv/bin/python -m build
.venv/bin/python -m pip install --no-deps dist/*.whl
.venv/bin/python -m pip check
```

Tests use mocked APIs and temporary encrypted stores, with no live mail calls.
CI runs these checks on Python 3.13 and 3.14 and imports the installed console
entrypoint without starting authentication or the stdio server.

## Security notes

- Each account's credentials live in **one Fernet-encrypted `auth_data` blob**
  (Google refresh token / MSAL token cache) — no secret material in the clear.
- `client_secret.json`, `accounts.db`, `fernet.key` are gitignored — never commit.
- Back up `fernet.key` alongside `accounts.db`; without the key the stored
  credentials are unrecoverable (you'd just re-connect the accounts).
- Existing email/default selectors remain unchanged. Provider collisions are
  rejected in either direction, including case variants. Use a distinct Microsoft
  alias when its primary address is already stored as Google.
- Microsoft records bind to MSAL's stable `home_account_id`. Legacy caches are
  bound lazily when there is one exact username match or a sole cached identity
  (the latter preserves existing alias selectors). Ambiguous/mismatched caches
  fail with reconnect guidance; they never fall back to the first cached user.
- Graph accepts only HTTPS `graph.microsoft.com` URLs and refuses redirects,
  including while following pagination links. Account-store creation is lazy;
  importing the server does not create or change credentials.
