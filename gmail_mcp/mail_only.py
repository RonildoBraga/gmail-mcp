"""Opt-in, account-scoped mail tools; the full local server remains available.

The default profile has no accounts and cannot access any mailbox. Configure
exact account selectors separately from existing OAuth credentials. This module
never initiates consent, discovers accounts or falls back to a default account.
"""

from __future__ import annotations

import argparse
import base64
import inspect
import json
import os
import re
import urllib.parse
from collections.abc import Iterable
from functools import wraps

from mcp.server.fastmcp import FastMCP

from . import graph
from . import server as full_server

READ_TOOLS = frozenset({
    "get_account_capabilities", "search_threads", "get_thread", "list_labels",
    "list_folders", "list_drafts", "list_filters", "get_filter",
    "list_inbox_rules", "get_inbox_rule",
})
ORGANIZE_TOOLS = frozenset({
    "create_label", "label_thread", "unlabel_thread", "archive_thread", "move_thread",
    "create_filter", "replace_filter", "delete_filter", "create_inbox_rule",
    "update_inbox_rule", "delete_inbox_rule",
})
MODES = ("read-only", "organize")
_GMAIL_ACTIONS = frozenset({"addLabelIds", "removeLabelIds"})
_GRAPH_ACTIONS = frozenset({"assignCategories", "markAsRead", "markImportance", "stopProcessingRules"})
_PROTECTED_LABELS = frozenset({"TRASH", "SPAM"})
_PROTECTED_FOLDERS = frozenset({"trash", "spam", "deleteditems", "junkemail"})


def _allowed_accounts(accounts: Iterable[str]) -> frozenset[str]:
    if isinstance(accounts, (str, bytes)):
        raise ValueError("allowed_accounts must be a collection of exact email selectors")
    permitted = frozenset(accounts)
    if any(not isinstance(account, str)
           or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account) is None
           or "*" in account for account in permitted):
        raise ValueError("Every allowed account must be an exact email selector; wildcards are unsupported")
    return permitted


def _labels(labels) -> None:
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ValueError("Label IDs must be a list of strings")
    if any(label.strip().upper() in _PROTECTED_LABELS for label in labels):
        raise ValueError("The mail-only profile cannot route mail to or from SPAM/TRASH")


def _gmail_filter(payload) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), dict):
        raise ValueError("A native Gmail filter with an action object is required")
    action = payload["action"]
    if set(action) - _GMAIL_ACTIONS:
        raise ValueError("The mail-only profile permits Gmail label actions only; forwarding is disabled")
    for labels in action.values():
        _labels(labels)


def _graph_actions(actions) -> None:
    if not isinstance(actions, dict) or set(actions) - _GRAPH_ACTIONS:
        raise ValueError(
            "The mail-only profile permits Outlook category/read/importance/stop actions only; "
            "sending, deletion and folder rule actions are disabled"
        )
    for name, value in actions.items():
        if name == "assignCategories":
            if not isinstance(value, list) or any(not isinstance(category, str) for category in value):
                raise ValueError("assignCategories must be a list of strings")
        elif name == "markImportance":
            if value not in ("low", "normal", "high"):
                raise ValueError("markImportance must be low, normal or high")
        elif not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")


def _guard_mutation(name: str, arguments: dict) -> None:
    if name in {"label_thread", "unlabel_thread"}:
        _labels(arguments["label_ids"])
    elif name in {"move_thread", "create_label"}:
        folder = arguments["folder"] if name == "move_thread" else arguments["name"]
        if not isinstance(folder, str) or re.sub(r"[\s_-]", "", folder).casefold() in _PROTECTED_FOLDERS:
            raise ValueError("The mail-only profile cannot route mail to automatic-purge folders")
        if name == "move_thread":
            _, provider, _ = full_server.auth.resolve_provider(arguments["account"])
            if provider == "microsoft" and folder.strip().casefold() not in {"archive", "inbox"}:
                # Display names can be localized or renamed. Do not guess which
                # custom-looking name resolves to an automatic-purge folder.
                raise ValueError("The mail-only profile supports Outlook moves to archive or inbox only")
            if provider == "google" and folder.strip().casefold() not in {"archive", ""}:
                for label in full_server.list_labels(account=arguments["account"])["labels"]:
                    if label["name"].casefold() == folder.casefold():
                        _labels([label["id"]])
    elif name in {"create_filter", "replace_filter"}:
        _gmail_filter(arguments["filter"])
    elif name == "create_inbox_rule":
        payload = arguments["rule"]
        _graph_actions(payload.get("actions") if isinstance(payload, dict) else None)
    elif name == "update_inbox_rule":
        patch = arguments["patch"]
        if not isinstance(patch, dict):
            raise ValueError("An Outlook rule patch object is required")
        if "actions" in patch:
            _graph_actions(patch["actions"])
            return  # Replacing actions with the permitted subset is allowed.
        if patch.get("isEnabled") is False:
            return  # Explicitly disabling an unsafe rule stops its mail actions.
        # Even a conditions-only PATCH can broaden an active forwarding rule.
        current = full_server.get_inbox_rule(arguments["rule_id"], account=arguments["account"])
        if (arguments.get("dry_run", True) is False
                and arguments.get("expected_version") != current["expected_version"]):
            raise ValueError("The Outlook rule changed; preview it again before applying")
        if patch.get("isEnabled") is True or current["rule"].get("isEnabled", True) is not False:
            _graph_actions(current["rule"].get("actions"))


def _guard_cursor(name: str, arguments: dict) -> None:
    """An opaque Graph cursor must not route an allowed token to another user."""
    if name not in {"search_threads", "list_drafts"} or not arguments.get("page_token"):
        return
    try:
        cursor = json.loads(base64.urlsafe_b64decode(arguments["page_token"].encode()).decode())
    except (ValueError, UnicodeError):
        return  # Gmail page tokens are provider-native opaque strings.
    if not isinstance(cursor, dict) or not {"scope", "pending", "next", "seen"}.issubset(cursor):
        return
    account = arguments["account"]
    scope = f"search:{account}:{arguments['query']}" if name == "search_threads" else f"drafts:{account}"
    if cursor["scope"] != scope:
        raise ValueError("Continuation token does not match this account and operation")
    if cursor["next"] is not None:
        url = graph._url(cursor["next"])
        expected_path = "/v1.0/me/messages" if name == "search_threads" else "/v1.0/me/mailFolders/drafts/messages"
        if urllib.parse.urlsplit(url).path != expected_path:
            raise ValueError("The mail-only profile cannot follow a cursor outside this account's mail collection")


def _guarded_tool(name: str, allowed: frozenset[str], mode: str):
    original = getattr(full_server, name)
    signature = inspect.signature(original, eval_str=True)
    parameters = [parameter.replace(default=inspect.Parameter.empty, annotation=str)
                  if parameter.name == "account" else parameter
                  for parameter in signature.parameters.values()]
    signature = signature.replace(parameters=parameters)

    @wraps(original)
    def guarded(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        account = arguments.get("account")
        # This check must precede provider selection, default lookup and token
        # loading. Exact matching deliberately does not resolve aliases/case.
        if not isinstance(account, str) or account not in allowed:
            raise ValueError("An explicit account in the mail-only allowlist is required")
        try:
            _guard_cursor(name, arguments)
            if name in ORGANIZE_TOOLS:
                _guard_mutation(name, arguments)
            result = getattr(full_server, name)(**arguments)
        except ValueError as exc:
            # The full local server's useful account-discovery diagnostic must
            # not disclose other selectors through the restricted profile.
            if str(exc).startswith("Unknown account ") and ". Connected:" in str(exc):
                raise ValueError(
                    "The approved account is not connected. Configure its existing credentials separately; "
                    "this profile never falls back to another account."
                ) from None
            raise
        if name == "get_account_capabilities":
            result = {**result, "capabilities": {
                **result["capabilities"], "send": False, "drive": False,
                "draft_creation": False, "organize": mode == "organize",
                "settings_mutations": mode == "organize",
            }, "profile": "mail-only", "mode": mode,
                "allowed_tools": sorted(READ_TOOLS | (ORGANIZE_TOOLS if mode == "organize" else frozenset())),
                "scope_note": "Stored OAuth scopes are informational; this profile restricts callable tools and accounts."}
        return result

    guarded.__signature__ = signature
    description = original.__doc__ or name
    if name == "move_thread":
        description = (
            "File a Gmail thread under a label, created if missing, or move an Outlook conversation. "
            "This profile permits Outlook destinations archive and inbox only; custom Outlook folders "
            "are unsupported. Gmail SPAM/TRASH destinations are blocked."
        )
    elif name in {"create_filter", "replace_filter"}:
        description += (
            "\n\nMail-only restrictions: action keys are limited to addLabelIds/removeLabelIds. "
            "SPAM/TRASH and forwarding actions are blocked."
        )
    elif name in {"create_inbox_rule", "update_inbox_rule"}:
        description += (
            "\n\nMail-only restrictions: permitted action keys are assignCategories, markAsRead, "
            "markImportance and stopProcessingRules. Forwarding, redirection, deletion and move/copy "
            "folder actions are blocked. Existing unsafe rules may be disabled or replaced with safe actions."
        )
    guarded.__doc__ = description + (
        "\n\nAn explicit account matching this profile's exact allowlist is required; "
        "the default account is never used."
    )
    return guarded


def create_server(allowed_accounts: Iterable[str] = (), mode: str = "read-only") -> FastMCP:
    """Build a fresh registry containing only the configured profile's tools.

    No accounts are permitted by default. The organize mode is an explicit
    operator decision; settings tools retain their preview-first semantics.
    Removing a filter/rule deletes its definition, never mailbox messages.
    """
    allowed = _allowed_accounts(allowed_accounts)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    scoped = FastMCP("gmail-mail-only")
    tools = READ_TOOLS | (ORGANIZE_TOOLS if mode == "organize" else frozenset())
    # A separate registry prevents weakening or pruning the full local launcher.
    # Unregistered tools are rejected by FastMCP on invocation, not just hidden.
    for name in sorted(tools if allowed else ()):
        scoped.add_tool(_guarded_tool(name, allowed, mode), name=name,
                        annotations=full_server.READ_ONLY if name in READ_TOOLS else full_server.MUTATING)
    return scoped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the account-scoped mail-only MCP profile (stdio)")
    parser.add_argument("--allow-account", action="append", metavar="EMAIL",
                        help="Exact permitted account; repeat for additional accounts. Overrides the environment list.")
    parser.add_argument("--mode", choices=MODES, default=os.environ.get("GMAIL_MCP_MODE", "read-only"),
                        help="Default read-only; organize explicitly enables guarded mailbox/settings changes.")
    options = parser.parse_args(argv)
    accounts = options.allow_account
    if accounts is None:
        accounts = [value.strip() for value in os.environ.get("GMAIL_MCP_ALLOWED_ACCOUNTS", "").split(",")
                    if value.strip()]
    try:
        scoped = create_server(accounts, options.mode)
    except ValueError as exc:
        parser.error(str(exc))
    scoped.run()


if __name__ == "__main__":
    main()
