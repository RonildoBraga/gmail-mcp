"""Static configuration shared across providers.

Only non-secret values live here. The Microsoft client id is a public
identifier for a *public client* app (device-code flow), so it is safe to keep
in source — there is no client secret.
"""

from __future__ import annotations

# --- Google -----------------------------------------------------------------
GOOGLE_SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/drive",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# --- Microsoft (Graph, device-code public client) ---------------------------
# App registration "gmail-mcp-graph" (any org dir + personal MS accounts).
MS_CLIENT_ID = "37784185-cdeb-4495-a5c7-babfa27a42d3"
# "common" lets both personal (Outlook.com/Hotmail) and work accounts sign in.
MS_AUTHORITY = "https://login.microsoftonline.com/common"
# Delegated scopes; the user consents to these once at first device-code login.
# offline_access (=> refresh token) is added by MSAL automatically.
MS_SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
