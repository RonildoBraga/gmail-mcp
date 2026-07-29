# gmail-mcp — multi-account, multi-provider mail MCP server

An [MCP](https://modelcontextprotocol.io) mail server that connects **multiple
accounts across providers** — **Google (Gmail)** and **Microsoft (Outlook/
Hotmail)** — behind one uniform set of tools. Every tool takes an optional
`account` (email); omit it to use the default. `search_all_threads` fans out
across **all** connected accounts in one call, mixing Gmail and Outlook.

Architecture in one line: a **token store keyed by email + provider** routes
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
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

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
| `search_threads` / `get_thread` | ✅ | ✅ |
| `search_all_threads` (cross-account, cross-provider) | ✅ | ✅ |
| `create_draft` / `list_drafts` / `send_message` | ✅ | ✅ |
| `archive_thread` / `move_thread` / `list_folders` | ✅ | ✅ |
| `list_labels` / `create_label` / `label_thread` / `unlabel_thread` | ✅ | ✖ (not yet) |
| `drive_*` (list/search/read/create/move/rename/trash) | ✅ | ✖ (Google only) |

`archive_thread` / `move_thread` unify filing across providers: Gmail moves
labels (remove `INBOX`; a folder name maps to a label), Outlook moves the
conversation between mail folders (well-known `Archive` / `Junk Email` /
`Deleted Items`, or a custom folder created on demand). Prefer a custom folder
over Outlook's built-in `Junk Email` / `Deleted Items`, which auto-purge.

Search syntax: Gmail uses Gmail query syntax (`is:unread from:a@b.com`);
Microsoft treats the query as free-text `$search`.

## Security notes

- Each account's credentials live in **one Fernet-encrypted `auth_data` blob**
  (Google refresh token / MSAL token cache) — no secret material in the clear.
- `client_secret.json`, `accounts.db`, `fernet.key` are gitignored — never commit.
- Back up `fernet.key` alongside `accounts.db`; without the key the stored
  credentials are unrecoverable (you'd just re-connect the accounts).
