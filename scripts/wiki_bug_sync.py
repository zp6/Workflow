"""Wiki change sync — polls wiki request artifacts and creates GitHub Issues.

Fetches the wiki page list via the canonical MCP endpoint, finds all BUG-NNN
entries newer than the last-seen cursor, and opens a GitHub Issue for each new
bug using the built-in GITHUB_TOKEN. With ``--include-community-requests`` it
also syncs promoted non-bug wiki artifacts that represent feature requests,
patch requests, docs/ops changes, branch refinements, or project-design
proposals.

Cursor state lives in `.agents/.wiki_bug_sync_cursor` (a single line: last
synced BUG number as an integer, e.g. ``3``).  The file is committed to the
repo so GHA runners start from a known state and the pipeline stays
idempotent across re-runs.

Non-bug request state lives in `.agents/.wiki_change_sync_seen.json` as a list
of wiki paths already bridged to GitHub. BUG pages keep their numeric cursor so
the historic bug lane stays stable.

Exit codes
----------
0   All new requests synced (or no new requests found).
1   MCP protocol / response-shape error.
2   MCP network error.
3   GitHub API error.

Usage
-----
    python scripts/wiki_bug_sync.py
    python scripts/wiki_bug_sync.py --url https://tinyassets.io/mcp
    python scripts/wiki_bug_sync.py --dry-run

Stdlib only — no third-party deps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://tinyassets.io/mcp"
DEFAULT_TIMEOUT = 20.0
GITHUB_API = "https://api.github.com"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "Jonnyton/Workflow")
MAX_CHANGE_BODY_CHARS = 12000

_REPO_ROOT = Path(__file__).resolve().parent.parent
CURSOR_PATH = _REPO_ROOT / ".agents" / ".wiki_bug_sync_cursor"
CHANGE_SEEN_PATH = _REPO_ROOT / ".agents" / ".wiki_change_sync_seen.json"

_SEVERITY_LABELS: dict[str, str] = {
    # Canonical `wiki action=file_bug` severities.
    "critical": "severity:critical",
    "major": "severity:major",
    "minor": "severity:minor",
    "cosmetic": "severity:cosmetic",
    # Legacy seed/runbook severities kept for already-authored pages.
    "low": "severity:low",
    "medium": "severity:medium",
    "high": "severity:high",
    "blocker": "severity:blocker",
}
_AUTO_LABEL = "auto-bug"
_AUTO_CHANGE_LABEL = "auto-change"
_DAEMON_REQUEST_LABEL = "daemon-request"
_PAYMENT_FREE_OK_LABEL = "payment:free-ok"
_WRITER_POOL_LABEL = "writer-pool:claude-codex"
_CHECKER_POLICY_LABEL = "checker:cross-family"
_GATE_REQUIRED_LABEL = "gate-required"
_PRIORITY_LOOP_DISCIPLINE_LABEL = "priority:loop-discipline"
_PRIORITY_PRIMITIVE_LAYER_LABEL = "priority:primitive-layer"
_PRIORITY_PRIMITIVE_SURFACE_LABEL = "priority:primitive-surface"
_DAEMON_REQUEST_LABELS = [
    _DAEMON_REQUEST_LABEL,
    _AUTO_CHANGE_LABEL,
    _PAYMENT_FREE_OK_LABEL,
    _WRITER_POOL_LABEL,
    _CHECKER_POLICY_LABEL,
    _GATE_REQUIRED_LABEL,
]

_BUG_ID_RE = re.compile(r"BUG-(\d+)", re.IGNORECASE)

_REQUEST_KIND_LABELS: dict[str, str] = {
    "bug": "request:bug",
    "feature": "request:feature",
    "patch": "request:patch",
    "docs-ops": "request:docs-ops",
    "branch-refinement": "request:branch-refinement",
    "project-design": "request:project-design",
}

_CHANGE_KIND_PREFIX: dict[str, str] = {
    "feature": "WIKI-FEATURE",
    "patch": "WIKI-PATCH",
    "docs-ops": "WIKI-DOCS",
    "branch-refinement": "WIKI-BRANCH",
    "project-design": "WIKI-DESIGN",
}

_ARCHITECTURAL_FILING_MARKERS = (
    "architecture",
    "architectural",
    "design-note",
    "design note",
    "project design",
    "operating-model",
    "operating model",
    "roadmap",
    "strategic",
    "substrate",
)

_LOOP_DISCIPLINE_MARKERS = (
    "auto-fix",
    "auto fix",
    "brain coherence",
    "brain update",
    "brain_update",
    "brainupdate",
    "checker",
    "community loop",
    "consensusengagementindex",
    "consensus-marker lint",
    "daemon request",
    "loop",
    "operator triage",
    "priority label",
    "required-reason",
    "triage burden",
    "wiki-bug-sync",
    "wiki-change-sync",
    "wiki_bug_sync",
)

_PRIMITIVE_LAYER_MARKERS = (
    "access",
    "architecture",
    "authority",
    "classifier",
    "design note",
    "gate ladder",
    "meaning",
    "primitive",
    "request contract",
    "substrate",
)

_PRIMITIVE_SURFACE_KINDS = {
    "bug",
    "branch-refinement",
    "feature",
    "patch",
}

_INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "wiki-bug-sync", "version": "1.0"},
    },
}
_INITIALIZED_NOTIF = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class SyncError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------------------
# MCP transport helpers
# ---------------------------------------------------------------------------


def _mcp_post(
    url: str,
    sid: str | None,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[dict | None, str | None]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "workflow-wiki-bug-sync/1.0",
    }
    if sid:
        headers["mcp-session-id"] = sid
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            new_sid = resp.headers.get("mcp-session-id") or sid
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise SyncError(2, f"network error on {url}: {exc}") from exc

    result: dict | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                result = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
        elif line.startswith("{"):
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                pass
    return result, new_sid


def _mcp_initialize(
    url: str,
    timeout: float,
    post_fn=None,
) -> str | None:
    _post = post_fn or _mcp_post
    resp, sid = _post(url, None, _INIT_PAYLOAD, timeout)
    if not resp or "result" not in resp:
        raise SyncError(1, f"MCP initialize failed: {resp!r}")
    _post(url, sid, _INITIALIZED_NOTIF, timeout)
    return sid


def _mcp_call_tool(
    url: str,
    sid: str | None,
    tool: str,
    args: dict[str, Any],
    timeout: float,
    post_fn=None,
) -> dict[str, Any]:
    _post = post_fn or _mcp_post
    payload = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    resp, _ = _post(url, sid, payload, timeout)
    if resp is None or "result" not in resp:
        raise SyncError(1, f"tools/call {tool!r} got no result: {resp!r}")
    if resp["result"].get("isError"):
        content = resp["result"].get("content", [])
        text = next((c["text"] for c in content if c.get("type") == "text"), "")
        raise SyncError(1, f"tools/call {tool!r} returned isError: {text[:300]}")
    return resp["result"]


def _parse_text_result(result: dict[str, Any]) -> str:
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item["text"]
    raise SyncError(1, "tool returned no text content")


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def read_cursor(cursor_path: Path = CURSOR_PATH) -> int:
    """Return last-synced BUG number (0 = never synced)."""
    try:
        return int(cursor_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_cursor(num: int, cursor_path: Path = CURSOR_PATH) -> None:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(str(num), encoding="utf-8")


def read_seen_changes(seen_path: Path = CHANGE_SEEN_PATH) -> set[str]:
    """Return wiki paths already synced through the non-bug request lane."""
    try:
        raw = json.loads(seen_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        paths = raw.get("seen_paths", [])
        if isinstance(paths, list):
            return {str(item) for item in paths}
    return set()


def write_seen_changes(paths: set[str], seen_path: Path = CHANGE_SEEN_PATH) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "seen_paths": sorted(paths),
    }
    seen_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Bug-page discovery
# ---------------------------------------------------------------------------


def _bug_number(path: str) -> int | None:
    """Extract integer bug number from a wiki path like 'bugs/BUG-003-...'."""
    m = _BUG_ID_RE.search(path)
    return int(m.group(1)) if m else None


def list_new_bugs(
    wiki_list: dict[str, Any],
    cursor: int,
) -> list[dict[str, Any]]:
    """Return promoted bug entries with bug_number > cursor, sorted ascending."""
    bugs = []
    for entry in wiki_list.get("promoted", []):
        if entry.get("type") != "bug":
            continue
        num = _bug_number(entry.get("path", ""))
        if num is None or num <= cursor:
            continue
        bugs.append({**entry, "bug_number": num})
    bugs.sort(key=lambda e: e["bug_number"])
    return bugs


def _change_kind(entry: dict[str, Any]) -> str | None:
    """Classify promoted non-bug wiki pages that should enter the change loop."""
    path = str(entry.get("path", "")).lower()
    entry_type = str(entry.get("type", "")).lower()
    title = str(entry.get("title", "")).lower()
    design_text = f"{path} {entry_type} {title}"
    routed_change_path = path.startswith((
        "pages/feature-requests/",
        "feature-requests/",
        "pages/patch-requests/",
        "patch-requests/",
        "pages/design-proposals/",
        "design-proposals/",
    ))

    # BUG pages are handled by the numeric cursor lane above.
    if (
        _bug_number(path) is not None
        or "/bugs/" in path
        or path.startswith("bugs/")
        or (entry_type == "bug" and not routed_change_path)
    ):
        return None

    if path.startswith("pages/notes/"):
        if "builder" in entry_type or "builder" in title:
            return "branch-refinement"
        return None

    if any(marker in design_text for marker in _ARCHITECTURAL_FILING_MARKERS):
        return "project-design"

    if (
        entry_type in {"feature", "feature_request"}
        or path.startswith("pages/feature-requests/")
        or path.startswith("feature-requests/")
        or path.startswith("pages/plans/feature-")
        or title.startswith("feature ")
    ):
        return "feature"
    if (
        entry_type in {"patch", "patch_request"}
        or path.startswith("pages/patch-requests/")
        or path.startswith("patch-requests/")
        or path.startswith("pages/plans/patch-")
        or title.startswith("patch ")
    ):
        return "patch"
    if (
        entry_type in {"design", "design_proposal", "project-design"}
        or path.startswith("pages/design-proposals/")
        or path.startswith("design-proposals/")
    ):
        return "project-design"
    if path.startswith("pages/workflows/"):
        return "branch-refinement"
    if path.startswith("pages/plans/") and any(
        marker in design_text
        for marker in (
            *_ARCHITECTURAL_FILING_MARKERS,
            "attribution",
            "design",
            "refactoring",
            "synthesis",
        )
    ):
        return "project-design"
    if path.startswith("pages/plans/") or path.startswith("pages/concepts/"):
        return "docs-ops"
    return None


def list_new_change_requests(
    wiki_list: dict[str, Any],
    seen_paths: set[str],
) -> list[dict[str, Any]]:
    """Return non-bug request artifacts not yet bridged to GitHub."""
    requests = []
    for entry in wiki_list.get("promoted", []):
        path = entry.get("path", "")
        if not path or path in seen_paths:
            continue
        kind = _change_kind(entry)
        if not kind:
            continue
        requests.append({**entry, "request_kind": kind})
    requests.sort(key=lambda e: e.get("path", ""))
    return requests


# ---------------------------------------------------------------------------
# Wiki read for bug detail
# ---------------------------------------------------------------------------


def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            body = parts[2].lstrip()
    return meta, body


def fetch_wiki_page_detail(
    url: str,
    sid: str | None,
    path: str,
    timeout: float,
    post_fn=None,
) -> dict[str, Any]:
    """Call wiki action=read and return parsed frontmatter plus page body."""
    page = Path(path).stem or path
    result = _mcp_call_tool(
        url, sid, "wiki", {"action": "read", "page": page}, timeout, post_fn
    )
    text = _parse_text_result(result)
    try:
        data = json.loads(text)
        content = data.get("content", "")
    except (json.JSONDecodeError, AttributeError):
        content = text

    meta, body = _split_frontmatter(content)
    return {"meta": meta, "body": body, "content": content}


def fetch_bug_detail(
    url: str,
    sid: str | None,
    path: str,
    timeout: float,
    post_fn=None,
) -> dict[str, Any]:
    """Call wiki action=read and parse the frontmatter fields we need."""
    return fetch_wiki_page_detail(url, sid, path, timeout, post_fn).get("meta", {})


def format_change_issue_body(entry: dict[str, Any], page_detail: dict[str, Any]) -> str:
    """Build bounded issue context for non-bug community request artifacts."""
    body = f"**Wiki type:** `{entry.get('type', 'unknown')}`"
    source_body = str(page_detail.get("body") or "").strip()
    if not source_body:
        return body

    truncated = source_body[:MAX_CHANGE_BODY_CHARS]
    if len(source_body) > MAX_CHANGE_BODY_CHARS:
        truncated = (
            f"{truncated}\n\n"
            f"_Source wiki body truncated at {MAX_CHANGE_BODY_CHARS} characters._"
        )
    return f"{body}\n\n## Source wiki body\n\n{truncated}"


# ---------------------------------------------------------------------------
# GitHub Issue creation
# ---------------------------------------------------------------------------


def _gh_ensure_label(
    token: str,
    repo: str,
    label: str,
    color: str = "e4e669",
    gh_api: str = GITHUB_API,
    timeout: float = 20.0,
) -> None:
    """Create the label if it doesn't exist yet. Best-effort."""
    url = f"{gh_api}/repos/{repo}/labels"
    body = json.dumps({"name": label, "color": color}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "workflow-wiki-bug-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            pass  # label already exists
    except (urllib.error.URLError, OSError):
        pass  # best-effort


def _label_color(label: str) -> str:
    if label == _AUTO_LABEL:
        return "0075ca"
    if label == _AUTO_CHANGE_LABEL:
        return "0e8a16"
    if label == _DAEMON_REQUEST_LABEL:
        return "0052cc"
    if label.startswith("payment:"):
        return "bfdadc"
    if label.startswith("writer-pool:"):
        return "1d76db"
    if label.startswith("checker:"):
        return "b60205"
    if label == _GATE_REQUIRED_LABEL:
        return "fbca04"
    if label.startswith("priority:"):
        return "f9d0c4"
    if label.startswith("severity:"):
        return "d93f0b"
    if label.startswith("request:"):
        return "5319e7"
    return "e4e669"


def priority_labels_for_request(
    request_kind: str,
    *,
    title: str = "",
    path: str = "",
    body_md: str = "",
) -> list[str]:
    """Return queue-priority labels promoted from request classifier signals."""
    corpus = " ".join((request_kind, title, path, body_md)).lower()
    if any(marker in corpus for marker in _LOOP_DISCIPLINE_MARKERS):
        return [_PRIORITY_LOOP_DISCIPLINE_LABEL]
    if (
        request_kind == "project-design"
        or any(marker in corpus for marker in _PRIMITIVE_LAYER_MARKERS)
    ):
        return [_PRIORITY_PRIMITIVE_LAYER_LABEL]
    if request_kind in _PRIMITIVE_SURFACE_KINDS:
        return [_PRIORITY_PRIMITIVE_SURFACE_LABEL]
    return []


def create_gh_issue(
    token: str,
    repo: str,
    bug_id: str,
    title: str,
    severity: str,
    component: str,
    body_md: str,
    dry_run: bool = False,
    gh_api: str = GITHUB_API,
    timeout: float = 20.0,
) -> str:
    """Create a GitHub Issue; returns the issue URL or '[dry-run]'."""
    labels = [_AUTO_LABEL, *_DAEMON_REQUEST_LABELS, _REQUEST_KIND_LABELS["bug"]]
    labels.extend(priority_labels_for_request(
        "bug", title=title, body_md=body_md,
    ))
    sev_label = _SEVERITY_LABELS.get(severity.lower())
    if sev_label:
        labels.append(sev_label)

    title_str = f"[{bug_id}] {title}"
    issue_body = (
        f"**Component:** {component}\n"
        f"**Severity:** {severity}\n\n"
        "**Daemon request contract:** claimable by paid or free daemons that meet "
        "the declared gate requirements. Code-change writers are Claude/Codex "
        "only and require an opposite-family checker.\n"
        "**Bounty terms:** no paid bounty is attached by default; if one is "
        "added, settlement follows the gate ladder's `bounty_requirements`.\n\n"
        f"{body_md}\n\n"
        f"_Auto-filed by wiki-bug-sync from wiki entry `{bug_id}`._"
    )

    if dry_run:
        print(f"[wiki-bug-sync] DRY-RUN: would create issue {title_str!r} labels={labels}")
        return "[dry-run]"

    if not token:
        raise SyncError(3, "GITHUB_TOKEN is not set — cannot create GH Issues")

    # Ensure labels exist
    for label in labels:
        _gh_ensure_label(
            token, repo, label, color=_label_color(label), gh_api=gh_api, timeout=timeout
        )

    url = f"{gh_api}/repos/{repo}/issues"
    payload = json.dumps({"title": title_str, "body": issue_body, "labels": labels}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "workflow-wiki-bug-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("html_url", "(no url)")
    except urllib.error.HTTPError as exc:
        body_err = exc.read().decode("utf-8", errors="replace")[:300]
        raise SyncError(3, f"GitHub API error {exc.code}: {body_err}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SyncError(3, f"GitHub network error: {exc}") from exc


def create_gh_change_issue(
    token: str,
    repo: str,
    request_kind: str,
    title: str,
    path: str,
    body_md: str,
    dry_run: bool = False,
    gh_api: str = GITHUB_API,
    timeout: float = 20.0,
) -> str:
    """Create a non-bug community change Issue."""
    kind_label = _REQUEST_KIND_LABELS.get(request_kind, "request:change")
    labels = [*_DAEMON_REQUEST_LABELS, kind_label]
    labels.extend(priority_labels_for_request(
        request_kind, title=title, path=path, body_md=body_md,
    ))
    prefix = _CHANGE_KIND_PREFIX.get(request_kind, "WIKI-CHANGE")
    title_str = f"[{prefix}] {title}"
    issue_body = (
        f"**Request kind:** {request_kind}\n"
        f"**Wiki path:** `{path}`\n\n"
        "**Daemon request contract:** claimable by paid or free daemons that meet "
        "the declared gate requirements. Code-change writers are Claude/Codex "
        "only and require an opposite-family checker.\n"
        "**Bounty terms:** no paid bounty is attached by default; if one is "
        "added, settlement follows the gate ladder's `bounty_requirements`.\n\n"
        f"{body_md}\n\n"
        f"_Auto-filed by wiki-change-sync from wiki page `{path}`._"
    )

    if dry_run:
        print(f"[wiki-bug-sync] DRY-RUN: would create issue {title_str!r} labels={labels}")
        return "[dry-run]"

    if not token:
        raise SyncError(3, "GITHUB_TOKEN is not set — cannot create GH Issues")

    for label in labels:
        _gh_ensure_label(
            token, repo, label, color=_label_color(label), gh_api=gh_api, timeout=timeout
        )

    url = f"{gh_api}/repos/{repo}/issues"
    payload = json.dumps({"title": title_str, "body": issue_body, "labels": labels}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "workflow-wiki-change-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("html_url", "(no url)")
    except urllib.error.HTTPError as exc:
        body_err = exc.read().decode("utf-8", errors="replace")[:300]
        raise SyncError(3, f"GitHub API error {exc.code}: {body_err}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SyncError(3, f"GitHub network error: {exc}") from exc


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------


def sync(
    url: str,
    timeout: float,
    dry_run: bool = False,
    include_community_requests: bool = False,
    token: str | None = None,
    repo: str = GITHUB_REPO,
    cursor_path: Path = CURSOR_PATH,
    change_seen_path: Path = CHANGE_SEEN_PATH,
    post_fn=None,
) -> int:
    """Run one sync pass. Returns exit code."""
    _token = token or os.environ.get("GITHUB_TOKEN", "")

    try:
        sid = _mcp_initialize(url, timeout, post_fn)

        # Fetch wiki list
        list_result = _mcp_call_tool(url, sid, "wiki", {"action": "list"}, timeout, post_fn)
        wiki_list = json.loads(_parse_text_result(list_result))

        cursor = read_cursor(cursor_path)
        new_bugs = list_new_bugs(wiki_list, cursor)
        seen_changes = read_seen_changes(change_seen_path)
        new_changes = (
            list_new_change_requests(wiki_list, seen_changes)
            if include_community_requests
            else []
        )

        if not new_bugs and not new_changes:
            if include_community_requests:
                print(
                    "[wiki-bug-sync] No new requests "
                    f"(bug_cursor={cursor}, change_seen={len(seen_changes)})"
                )
            else:
                print(f"[wiki-bug-sync] No new bugs (cursor={cursor})")
            return 0

        if new_bugs:
            print(f"[wiki-bug-sync] {len(new_bugs)} new bug(s) since BUG-{cursor:03d}")
        if new_changes:
            print(f"[wiki-bug-sync] {len(new_changes)} new community request(s)")

        max_synced = cursor
        for entry in new_bugs:
            bug_num = entry["bug_number"]
            path = entry["path"]
            bug_id = f"BUG-{bug_num:03d}"

            # Fetch detail for richer issue body
            try:
                meta = fetch_bug_detail(url, sid, path, timeout, post_fn)
            except SyncError:
                meta = {}

            title = meta.get("title") or entry.get("title") or bug_id
            severity = meta.get("severity", "medium")
            component = meta.get("component", "unknown")
            body_md = f"**Wiki path:** `{path}`"

            issue_url = create_gh_issue(
                _token, repo, bug_id, title, severity, component, body_md,
                dry_run=dry_run, timeout=timeout,
            )
            print(f"[wiki-bug-sync] {bug_id} -> {issue_url}")
            max_synced = max(max_synced, bug_num)

        for entry in new_changes:
            path = entry["path"]
            request_kind = entry["request_kind"]

            try:
                page_detail = fetch_wiki_page_detail(url, sid, path, timeout, post_fn)
                meta = page_detail.get("meta", {})
            except SyncError:
                page_detail = {}
                meta = {}

            title = meta.get("title") or entry.get("title") or Path(path).stem
            body_md = format_change_issue_body(entry, page_detail)
            issue_url = create_gh_change_issue(
                _token,
                repo,
                request_kind,
                title,
                path,
                body_md,
                dry_run=dry_run,
                timeout=timeout,
            )
            print(f"[wiki-bug-sync] {request_kind}:{path} -> {issue_url}")
            seen_changes.add(path)

        if not dry_run:
            if max_synced != cursor:
                write_cursor(max_synced, cursor_path)
                print(f"[wiki-bug-sync] bug cursor updated to {max_synced}")
            if include_community_requests:
                write_seen_changes(seen_changes, change_seen_path)
                print(f"[wiki-bug-sync] change seen updated to {len(seen_changes)} paths")

        return 0

    except SyncError as exc:
        print(f"[wiki-bug-sync] FAIL (exit {exc.code}): {exc.msg}", file=sys.stderr)
        return exc.code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sync wiki BUG-NNN entries to GitHub Issues."
    )
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default: {DEFAULT_URL})")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"Per-request timeout (default {DEFAULT_TIMEOUT}s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be created without touching GH or cursor")
    ap.add_argument("--include-community-requests", action="store_true",
                    help="Also sync promoted non-bug wiki request artifacts")
    ap.add_argument("--repo", default=GITHUB_REPO, help=f"owner/repo (default: {GITHUB_REPO})")
    args = ap.parse_args(argv)
    return sync(
        args.url,
        args.timeout,
        dry_run=args.dry_run,
        include_community_requests=args.include_community_requests,
        repo=args.repo,
    )


if __name__ == "__main__":
    sys.exit(main())
