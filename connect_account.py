"""One-off helper: run the OAuth flow but PRINT the consent URL instead of
opening a browser, so the consent can be completed in a specific browser
profile (the one signed into the intended account).

Usage:
    .venv\\Scripts\\python.exe connect_account.py

It prints a line `AUTHURL <url>`. Open that URL in the browser/profile signed
into the account you want to connect, approve, and this process captures the
token (redirect lands on the local loopback server it's running).
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_mcp import auth

flow = InstalledAppFlow.from_client_secrets_file(str(auth.CLIENT_SECRET_PATH), auth.SCOPES)
creds = flow.run_local_server(
    port=0,
    open_browser=False,
    authorization_prompt_message="AUTHURL {url}",
    access_type="offline",
    prompt="consent",
)
email = auth._email_for(creds)
auth._persist(email, creds)
print("CONNECTED", email)
sys.stdout.flush()
