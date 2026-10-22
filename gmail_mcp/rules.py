"""Native Gmail filters and Microsoft Inbox rules, with previews and recovery data.

Mutating tools must resolve an explicit account before calling these helpers.
Destructive applies also require the version returned by a preview/get, bound to
the resolved account, provider, target, and current server representation. These
are optimistic checks, not transactions: neither provider offers atomic replace.
No mutation is retried, including when a response may have been lost.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from urllib.parse import quote

from . import graph

_RULES = "/me/mailFolders/inbox/messageRules"
_RULE_FIELDS = {"displayName", "sequence", "isEnabled", "conditions", "exceptions", "actions"}


class RulePermissionError(PermissionError):
    """Permission guidance that retains the provider status for mutation outcomes."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _base(provider) -> dict:
    return {"account": provider.email, "provider": provider.provider}


def _require(provider, expected: str) -> None:
    if provider.provider != expected:
        name = "Gmail filters" if expected == "google" else "Outlook Inbox rules"
        raise ValueError(f"{name} are unsupported for {provider.provider!r} account {provider.email!r}.")


def _target(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A non-empty filter/rule ID is required.")
    return value


def _version(provider, target: str, snapshot: dict) -> str:
    raw = json.dumps([provider.provider, provider.email, target, snapshot], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _check_version(provider, target: str, snapshot: dict, expected: str | None) -> None:
    if not expected or expected != _version(provider, target, snapshot):
        raise ValueError("Missing or stale expected_version. Preview again using the same explicit account.")


def _status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    status = status or getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\((\d{3})\)", str(exc))
    return int(match[1]) if match else None


def _error(exc: Exception, provider) -> str:
    detail = str(exc)
    if _status(exc) in (401, 403) and "No consent or scope expansion" not in detail:
        scope = "gmail.settings.basic" if provider.provider == "google" else "MailboxSettings.ReadWrite"
        detail += (f" Verify this account's existing {scope} permission; reconnect it explicitly if "
                   "permission is missing. No consent or scope expansion was attempted.")
    return detail


def _failed(exc: Exception, provider) -> dict:
    # A provider's explicit client error is a denial. A transport error or server
    # failure might follow a committed write; never claim that it did not happen.
    status = _status(exc)
    return {"status": "failed" if status and 400 <= status < 500 and status != 408 else "uncertain",
            "error": _error(exc, provider), "retry_safe": False,
            "guidance": "Inspect current provider state before retrying; no automatic retry was made."}


def _google(provider, method: str, **kwargs) -> dict:
    try:
        resource = provider.svc.users().settings().filters()
        return getattr(resource, method)(userId="me", **kwargs).execute(num_retries=0)
    except Exception as exc:
        if _status(exc) in (401, 403):
            raise RulePermissionError(_error(exc, provider), _status(exc)) from exc
        raise


def _graph(provider, method: str, path: str, body: dict | None = None) -> dict:
    try:
        return graph._request(provider.token, method, path, body)
    except Exception as exc:
        if _status(exc) in (401, 403):
            raise RulePermissionError(_error(exc, provider), _status(exc)) from exc
        raise


def _filter_body(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"criteria", "action"}:
        raise ValueError("A Gmail filter accepts only native criteria and action objects; omit id.")
    if not isinstance(value.get("criteria"), dict) or not isinstance(value.get("action"), dict):
        raise ValueError("A Gmail filter requires criteria and action objects.")
    if not value["action"]:
        raise ValueError("A Gmail filter requires a non-empty action object.")
    return copy.deepcopy(value)


def _rule_body(value: dict, *, create: bool = False) -> dict:
    if not isinstance(value, dict) or not value or set(value) - _RULE_FIELDS:
        raise ValueError("Use native writable rule fields only: " + ", ".join(sorted(_RULE_FIELDS)))
    if create and not {"displayName", "sequence", "actions"} <= value.keys():
        raise ValueError("Creating an Inbox rule requires displayName, sequence, and actions.")
    if "displayName" in value and (not isinstance(value["displayName"], str) or not value["displayName"].strip()):
        raise ValueError("displayName must be a non-empty string.")
    if "sequence" in value and (type(value["sequence"]) is not int or not 0 <= value["sequence"] < 2**31):
        raise ValueError("sequence must be a non-negative Int32.")
    if "isEnabled" in value and type(value["isEnabled"]) is not bool:
        raise ValueError("isEnabled must be a boolean.")
    for field in ("actions", "conditions", "exceptions"):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f"{field} must be a native object.")
    if create and not value["actions"]:
        raise ValueError("Creating an Inbox rule requires non-empty actions.")
    return copy.deepcopy(value)


def _rule_restore(snapshot: dict) -> dict:
    return {key: copy.deepcopy(value) for key, value in snapshot.items() if key in _RULE_FIELDS}


def _created_id(response) -> str | None:
    """A malformed success body cannot prove which object was created."""
    value = response.get("id") if isinstance(response, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _writable(snapshot: dict) -> None:
    if snapshot.get("isReadOnly"):
        raise ValueError("This Outlook rule isReadOnly and cannot be modified or deleted through Graph.")


def list_filters(provider) -> dict:
    _require(provider, "google")
    # Gmail filters.list has no paging parameters or continuation token.
    return {**_base(provider), "filters": _google(provider, "list").get("filter", [])}


def get_filter(provider, filter_id: str) -> dict:
    _require(provider, "google")
    snapshot = _google(provider, "get", id=_target(filter_id))
    return {**_base(provider), "filter": snapshot,
            "expected_version": _version(provider, filter_id, snapshot)}


def create_filter(provider, filter: dict, dry_run: bool = True) -> dict:
    _require(provider, "google")
    body = _filter_body(filter)
    result = {**_base(provider), "operation": "create_filter", "dry_run": dry_run, "filter": body}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "filter": body, "dry_run": False}}
    try:
        created = _google(provider, "create", body=body)
    except Exception as exc:
        return {**result, **_failed(exc, provider)}
    created_id = _created_id(created)
    if created_id is None:
        return {**result, "status": "uncertain", "retry_safe": False,
                "guidance": "Create returned no ID; inspect filters before retrying."}
    return {**result, "status": "created", "filter": created,
            "recovery": {"operation": "delete_filter", "account": provider.email,
                         "filter_id": created_id, "preview_required": True}}


def delete_filter(provider, filter_id: str, dry_run: bool = True, expected_version: str | None = None) -> dict:
    current = get_filter(provider, filter_id)
    snapshot = current["filter"]
    body = {key: copy.deepcopy(snapshot[key]) for key in ("criteria", "action") if key in snapshot}
    result = {**_base(provider), "operation": "delete_filter", "filter_id": filter_id,
              "dry_run": dry_run, "before": snapshot, "expected_version": current["expected_version"],
              "recovery": {"operation": "create_filter", "account": provider.email, "filter": body,
                           "note": "Recreation assigns a new ID; it does not undo prior mail actions."}}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "filter_id": filter_id, "dry_run": False,
                                   "expected_version": current["expected_version"]}}
    _check_version(provider, filter_id, snapshot, expected_version)
    try:
        _google(provider, "delete", id=filter_id)
    except Exception as exc:
        return {**result, **_failed(exc, provider)}
    return {**result, "status": "deleted"}


def replace_filter(provider, filter_id: str, filter: dict, dry_run: bool = True,
                   expected_version: str | None = None) -> dict:
    body = _filter_body(filter)
    current = get_filter(provider, filter_id)
    snapshot = current["filter"]
    restore = {key: copy.deepcopy(snapshot[key]) for key in ("criteria", "action") if key in snapshot}
    result = {**_base(provider), "operation": "replace_filter", "filter_id": filter_id,
              "dry_run": dry_run, "before": snapshot, "filter": body,
              "expected_version": current["expected_version"],
              "recovery": {"account": provider.email, "old_filter_id": filter_id, "old_filter": restore},
              "note": "Create first, then delete the old filter. Both may briefly be active."}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "filter_id": filter_id, "filter": body,
                                   "dry_run": False, "expected_version": current["expected_version"]}}
    _check_version(provider, filter_id, snapshot, expected_version)
    try:
        created = _google(provider, "create", body=body)
    except Exception as exc:
        return {**result, **_failed(exc, provider), "old_filter_id": filter_id,
                "old_filter_delete_attempted": False, "new_filter_id": None}
    new_id = _created_id(created)
    if not new_id or new_id == filter_id:
        return {**result, "status": "uncertain", "old_filter_id": filter_id, "new_filter_id": new_id,
                "old_filter_delete_attempted": False, "retry_safe": False,
                "guidance": "Unexpected create response; inspect filters before retrying."}
    result.update({"old_filter_id": filter_id, "new_filter_id": new_id, "created_filter": created})
    result["recovery"]["new_filter_id"] = new_id
    try:
        _google(provider, "delete", id=filter_id)
    except Exception as exc:
        failure = _failed(exc, provider)
        return {**result, **failure, "status": "partial", "delete_outcome": failure["status"],
                "guidance": "New filter exists. Inspect both IDs before retrying deletion or removing the new filter."}
    return {**result, "status": "replaced"}


def list_inbox_rules(provider) -> dict:
    _require(provider, "microsoft")
    path, seen, values = _RULES, set(), []
    while path:
        if path in seen:
            raise RuntimeError("Graph Inbox rule pagination loop detected.")
        seen.add(path)
        page = _graph(provider, "GET", path)
        values.extend(page.get("value", []))
        path = page.get("@odata.nextLink")
    return {**_base(provider), "rules": values}


def get_inbox_rule(provider, rule_id: str) -> dict:
    _require(provider, "microsoft")
    snapshot = _graph(provider, "GET", _RULES + "/" + quote(_target(rule_id), safe=""))
    return {**_base(provider), "rule": snapshot,
            "expected_version": _version(provider, rule_id, snapshot)}


def create_inbox_rule(provider, rule: dict, dry_run: bool = True) -> dict:
    _require(provider, "microsoft")
    body = _rule_body(rule, create=True)
    result = {**_base(provider), "operation": "create_inbox_rule", "dry_run": dry_run, "rule": body}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "rule": body, "dry_run": False}}
    try:
        created = _graph(provider, "POST", _RULES, body)
    except Exception as exc:
        return {**result, **_failed(exc, provider)}
    created_id = _created_id(created)
    if created_id is None:
        return {**result, "status": "uncertain", "retry_safe": False,
                "guidance": "Create returned no ID; inspect Inbox rules before retrying."}
    return {**result, "status": "created", "rule": created,
            "recovery": {"operation": "delete_inbox_rule", "account": provider.email,
                         "rule_id": created_id, "preview_required": True,
                         "note": "Deleting cannot undo mail actions or provider sequence recalculation."}}


def update_inbox_rule(provider, rule_id: str, patch: dict, dry_run: bool = True,
                      expected_version: str | None = None) -> dict:
    body = _rule_body(patch)
    current = get_inbox_rule(provider, rule_id)
    snapshot = current["rule"]
    _writable(snapshot)
    # Undo only fields this patch changed. Replaying the whole rule can erase
    # independent edits or reorder rules during an otherwise unrelated undo.
    restore = {field: copy.deepcopy(snapshot[field]) for field in body if field in snapshot}
    for field in ("conditions", "exceptions"):
        if field in body and field not in snapshot:
            restore[field] = {}
    result = {**_base(provider), "operation": "update_inbox_rule", "rule_id": rule_id, "dry_run": dry_run,
              "before": snapshot, "patch": body, "expected_version": current["expected_version"],
              "recovery": {"operation": "update_inbox_rule", "account": provider.email, "rule_id": rule_id,
                           "patch": restore, "preview_required": True}}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "rule_id": rule_id, "patch": body,
                                   "dry_run": False, "expected_version": current["expected_version"]}}
    _check_version(provider, rule_id, snapshot, expected_version)
    try:
        updated = _graph(provider, "PATCH", _RULES + "/" + quote(rule_id, safe=""), body)
    except Exception as exc:
        return {**result, **_failed(exc, provider)}
    return {**result, "status": "updated", "rule": updated}


def delete_inbox_rule(provider, rule_id: str, dry_run: bool = True,
                      expected_version: str | None = None) -> dict:
    current = get_inbox_rule(provider, rule_id)
    snapshot = current["rule"]
    _writable(snapshot)
    result = {**_base(provider), "operation": "delete_inbox_rule", "rule_id": rule_id, "dry_run": dry_run,
              "before": snapshot, "expected_version": current["expected_version"],
              "recovery": {"operation": "create_inbox_rule", "account": provider.email,
                           "rule": _rule_restore(snapshot),
                           "note": "Recreation assigns a new ID; inspect ordering and mail actions separately."}}
    if dry_run:
        return {**result, "apply": {"account": provider.email, "rule_id": rule_id, "dry_run": False,
                                   "expected_version": current["expected_version"]}}
    _check_version(provider, rule_id, snapshot, expected_version)
    try:
        _graph(provider, "DELETE", _RULES + "/" + quote(rule_id, safe=""))
    except Exception as exc:
        return {**result, **_failed(exc, provider)}
    return {**result, "status": "deleted"}
