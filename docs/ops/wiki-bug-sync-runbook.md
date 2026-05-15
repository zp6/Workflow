---
title: Wiki change sync runbook
date: 2026-04-21
status: active
---

# Wiki change sync runbook

`scripts/wiki_bug_sync.py` + `.github/workflows/wiki-bug-sync.yml`

Polls the wiki every 15 min for new BUG-NNN entries and promoted non-bug
community request artifacts, then opens GitHub Issues. This is the detect half
of the community change loop.

## How it works

1. GHA runs `wiki_bug_sync.py` every 15 min and on workflow/script pushes.
2. Script calls `wiki action=list` on `https://tinyassets.io/mcp`.
3. BUG lane: filters promoted BUG entries with `bug_number > cursor`.
4. Community lane: filters promoted non-bug request artifacts not in
   `.agents/.wiki_change_sync_seen.json`.
5. For each new item, calls `wiki action=read` to pull frontmatter. Non-bug
   community request issues also include a bounded copy of the source wiki body
   so auto-change writers see the request evidence without a separate lookup.
   Then the sync POSTs a GH Issue via `GITHUB_TOKEN`.
6. BUG issue title: `[BUG-NNN] <title>`. Labels: `daemon-request`,
   `auto-change`, `auto-bug`, `request:bug`, `payment:free-ok`,
   `writer-pool:claude-codex`, `checker:cross-family`, `gate-required`, and
   classifier-promoted `priority:*` plus `severity:<level>`.
7. Non-bug issue title: `[WIKI-<KIND>] <title>`. Labels: `daemon-request`,
   `auto-change`, `request:<kind>`, `payment:free-ok`,
   `writer-pool:claude-codex`, `checker:cross-family`, `gate-required`, and
   classifier-promoted `priority:*`.
8. Commits updated sync state back with `[skip ci]`.

## State files

BUG cursor:

```text
.agents/.wiki_bug_sync_cursor
```

Contents: a single integer, the last BUG number successfully synced.

Non-bug seen file:

```text
.agents/.wiki_change_sync_seen.json
```

Contents: JSON with a sorted `seen_paths` list. The initial committed file
seeds current promoted non-bug pages so the first generalized run does not
backfill the whole wiki into GitHub.

Both files are committed to the repo. GHA reads HEAD state, runs sync, then
commits increments back. This makes reruns idempotent.

## Request kinds

| Wiki artifact shape | GH label |
|---|---|
| BUG page | `request:bug` |
| `pages/plans/feature-*` | `request:feature` |
| `pages/plans/patch-*` | `request:patch` |
| `pages/workflows/*` | `request:branch-refinement` |
| builder notes under `pages/notes/*` | `request:branch-refinement` |
| other coordination notes under `pages/notes/*` | ignored by sync |
| strategic/roadmap/design/architecture/refactoring/attribution plans | `request:project-design` |
| patch/feature pages whose title/path has architectural shape | `request:project-design` |
| other promoted plans/concepts | `request:docs-ops` |

Architectural shape wins over the folder name. A patch request asking for
architecture, design-note, roadmap, operating-model, or substrate changes is
filed as `request:project-design`; the auto-fix workflow then drafts under
`docs/design-notes/proposed/` on a `design-note-draft/*` branch instead of
opening a mechanical `auto-change/*` branch.

Ordinary `pages/notes/*` coordination artifacts are not request artifacts.
Only builder notes enter the branch-refinement lane; design or architecture
words in coordination-note titles are not enough to create daemon requests.

## Manual trigger

Via GitHub Actions UI: Actions -> "Wiki bug sync" -> Run workflow.

Or via CLI:

```bash
GITHUB_TOKEN=$(gh auth token) python scripts/wiki_bug_sync.py \
    --url https://tinyassets.io/mcp \
    --repo Jonnyton/Workflow \
    --include-community-requests
```

## Dry run

```bash
python scripts/wiki_bug_sync.py \
    --dry-run \
    --url https://tinyassets.io/mcp \
    --repo Jonnyton/Workflow \
    --include-community-requests
```

Prints what would be created. It does not touch GitHub or update state files.

## Timestamp lint for BrainUpdate pages

BU-002 coordination pages use a source-fidelity split: typed coordination
timestamps must be UTC ISO8601 `Z` values, while source/canon prose keeps the
source's original date conventions. Before bridging or reviewing BrainUpdate
pages that add typed timestamp metadata, run:

```bash
python scripts/timestamp_lint_run.py path/to/page.md
```

For replaying wiki evidence against the observed write time, pass the server
write timestamp explicitly:

```bash
python scripts/timestamp_lint_run.py path/to/page.md \
    --write-time 2026-05-08T01:25:00Z
```

The check rejects typed timestamp fields in frontmatter or YAML state blocks
that are non-UTC, lack the `Z` suffix, or are future-stamped beyond the small
clock-skew tolerance. Prose dates and untyped source text are advisory only and
are intentionally ignored.

## Reset and skip

Reset BUG cursor:

```bash
echo 0 > .agents/.wiki_bug_sync_cursor
git add .agents/.wiki_bug_sync_cursor
git commit -m "chore: reset wiki-bug-sync cursor"
git push
```

Skip BUG range:

```bash
echo 45 > .agents/.wiki_bug_sync_cursor
git add .agents/.wiki_bug_sync_cursor
git commit -m "chore: advance wiki-bug-sync cursor to BUG-045"
git push
```

Skip a non-bug page by adding its wiki path to
`.agents/.wiki_change_sync_seen.json` and committing that file.

## Labels

Labels are created automatically if missing.

| Label family | Color |
|---|---|
| `daemon-request` | `0052cc` |
| `auto-bug` | `0075ca` |
| `auto-change` | `0e8a16` |
| `request:*` | `5319e7` |
| `severity:*` | `d93f0b` |
| `payment:*` | `bfdadc` |
| `writer-pool:*` | `1d76db` |
| `checker:*` | `b60205` |
| `gate-required` | `fbca04` |
| `priority:*` | `f9d0c4` |

## Request contract

`daemon-request` is the public work-order label. It means a paid or free
daemon may claim the request if it satisfies the request's gate requirements.
`payment:free-ok` means no paid bounty is attached by default; if a bounty is
later attached, its settlement terms must be encoded in the relevant gate
ladder's `bounty_requirements`. Code-change writers are restricted to the
Claude/Codex pool and require an opposite-family checker.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow runs but no issues created | Cursor/seen state already covers all items | Check state files; reset deliberately if needed |
| `GITHUB_TOKEN is not set` error | Token not passed | Use `--dry-run` locally; GHA provides token automatically |
| `MCP network error` | Daemon/canary unreachable | Check uptime-canary workflow for P0 status |
| Duplicate BUG issues | Cursor not committed back | Check GHA job logs for sync-state commit errors |
| Duplicate non-bug issues | Seen file not committed back | Check GHA job logs for sync-state commit errors |
| Labels not applied | Label create race | Re-run; label creation is idempotent on 422 |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All new requests synced, or none found |
| 1 | MCP protocol / response-shape error |
| 2 | MCP network error |
| 3 | GitHub API error |
