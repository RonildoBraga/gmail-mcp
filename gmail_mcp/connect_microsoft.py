"""Connect a Microsoft (Outlook/Hotmail) account via the device-code flow.

Run:  python -m gmail_mcp.connect_microsoft [key-email]

It prints a URL and a code; open the URL, enter the code, sign in and consent.
Pass an optional [key-email] to store the account under a specific address --
needed when the Microsoft primary alias collides with an existing Google account
(e.g. store the Outlook.com address explicitly).
"""

from __future__ import annotations

import sys

from . import auth


def main() -> None:
    key_email = sys.argv[1] if len(sys.argv) > 1 else None
    email = auth.add_microsoft_account(lambda message: print(message + "\n", flush=True), key_email=key_email)
    print(f"CONNECTED {email}", flush=True)


if __name__ == "__main__":
    main()
