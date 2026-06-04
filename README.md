# gmail-mcp — multi-account Gmail MCP server

A Gmail [MCP](https://modelcontextprotocol.io) server that supports **multiple
Google accounts** at once. Every tool takes an optional `account` (email);
omit it to use the default. `search_all_threads` fans out across all connected
accounts in one call.

The architecture in one line: OAuth is one-token-per-account, so this server
keeps a **token store keyed by email** and routes each call to the right token,
instead of holding a single global token.

## Layout

```
gmail-mcp/
├── gmail_mcp/
│   ├── store.py    # SQLite token store, keyed by email; refresh tokens encrypted (Fernet)
│   ├── auth.py     # OAuth consent flow (add_account) + resolve(account) -> live creds
│   ├── gmail.py    # Gmail API helpers; service_for(account) is the one routing point
│   ├── server.py   # FastMCP server; all tools
│   └── __main__.py # `python -m gmail_mcp`
├── credentials/    # client_secret.json (you add) + accounts.db + fernet.key (runtime). All gitignored.
└── requirements.txt
```

## One-time setup

### 1. Google Cloud OAuth client

1. [console.cloud.google.com](https://console.cloud.google.com) → create/select a project.
2. **APIs & Services → Enable APIs** → enable **Gmail API**.
3. **OAuth consent screen** → External → add yourself as a **Test user**
   (test mode is fine; you don't need verification for personal use).
4. **Credentials → Create Credentials → OAuth client ID → Desktop app**.
5. Download the JSON, save it as `credentials/client_secret.json`.

The **Desktop app** client type matters — it enables the loopback redirect that
`add_account` uses (`run_local_server`), so no redirect URI config is needed.

### 2. Python env (pyenv-win + venv, per PYTHON_SETUP.md)

```powershell
cd C:\Users\ronildo\Developer\gmail-mcp
pyenv local 3.13.13
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Connecting accounts

`add_account` is itself an MCP tool — but the first connect opens a browser for
consent, so it's easiest to run it once from the shell before wiring the server
into a client:

```powershell
.venv\Scripts\python.exe -c "from gmail_mcp import auth; print(auth.add_account())"
```

A browser opens → pick the Google account → approve. The server reads the
account's email from its Gmail profile and stores the tokens. Repeat for each
account. The **first** account connected becomes the default.

### If the wrong browser profile opens

`add_account` opens your **default** browser, which may be the wrong Chrome
profile (e.g. a work profile not signed into the account you want, or one whose
account isn't a test user → `403 access_denied`). Use the URL-based helper
instead — it prints the consent URL rather than opening a browser, so you can
paste it into the exact profile signed into the right account:

```powershell
$env:PYTHONUNBUFFERED='1'; .venv\Scripts\python.exe -u connect_account.py
```

It prints `AUTHURL <url>`. Open that URL in the correct browser/profile, approve,
and the loopback redirect completes the flow (prints `CONNECTED <email>`).

> Testing-mode gotcha: the consent shows "Google hasn't verified this app"
> (Advanced → Continue) and the Gmail permission must actually be granted — if
> only the sign-in/email scope comes back, the connect fails with a scope error.
> If a partial grant gets cached, revoke the app at
> [myaccount.google.com permissions](https://myaccount.google.com/permissions)
> and reconnect.

## Wiring into an MCP client

stdio server. Add to your MCP client config (e.g. Claude Code's `mcp` settings):

```json
{
  "mcpServers": {
    "gmail-multi": {
      "command": "C:\\Users\\ronildo\\Developer\\gmail-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "gmail_mcp"],
      "cwd": "C:\\Users\\ronildo\\Developer\\gmail-mcp"
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `add_account` | OAuth-connect a new Google account (opens browser) |
| `list_accounts` | List connected accounts + which is default |
| `set_default_account` | Choose the account used when `account` is omitted |
| `remove_account` | Disconnect an account, delete its tokens |
| `search_threads` | Search one account (Gmail query syntax) |
| `get_thread` | Full thread: messages, headers, decoded bodies |
| `search_all_threads` | **Cross-account** search across every connected account |
| `list_labels` / `create_label` | Label management |
| `label_thread` / `unlabel_thread` | Add/remove labels on a thread |
| `create_draft` / `list_drafts` | Draft management |

Every Gmail tool accepts `account="you@example.com"`. Omit it to hit the default.

## Scopes

`gmail.modify` (read + labels + threads + drafts) plus `openid` + `userinfo.email`
for account identity — see `auth.SCOPES`. Sending is intentionally **not**
granted; add `gmail.send` and a send tool if you need it. Changing scopes
requires re-running `add_account` for each account.

## Security notes

- Refresh tokens (long-lived) are **encrypted at rest** with Fernet; the key
  lives in `credentials/fernet.key` (gitignored). Access tokens are short-lived
  and stored plaintext.
- `client_secret.json`, `accounts.db`, and `fernet.key` are all gitignored —
  never commit them.
- Back up `fernet.key` if you back up `accounts.db`: without the key the stored
  refresh tokens are unrecoverable (you'd just re-run `add_account`).
