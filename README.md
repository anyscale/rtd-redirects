# rtd-redirects

Manage [Read the Docs](https://readthedocs.com/) redirects as code. A YAML file in your docs repo is the source of truth; this CLI reconciles it against the [RtD v3 API](https://docs.readthedocs.com/platform/latest/api/v3.html).

## Status

v0.1.0 in development. Built for `docs.ray.io` IA-cleanup campaigns; the patterns generalize to any RtD project. Design rationale and full architecture are in [`anyscale/docs:strategy/ray-docs/redirect-mgmt/`](https://github.com/anyscale/docs/blob/master/strategy/ray-docs/redirect-mgmt/).

## Why

Read the Docs has no bulk redirect import. Its dashboard UI requires clicking through each entry by hand, which makes any meaningful slug-rename or IA-cleanup campaign untenable. `rtd-redirects` reads a YAML file from your repo, diffs it against the live RtD state, and applies the diff. PR-time mode produces a git-only diff with no API calls. Merge-time mode applies via API.

## Install

```bash
git clone git@github.com:anyscale/rtd-redirects.git
cd rtd-redirects
pip install -e .
```

Once `0.1.0` is published to PyPI, the installation simplifies to `pip install rtd-redirects`.

## Quick start

```bash
# Auth: token never goes to disk; read from 1Password (or your secret store) into env.
export RTD_API_TOKEN=$(op read "op://Personal/RtD/api-token")

# Optional: avoid passing --project on every call.
export RTD_PROJECT_SLUG=anyscale-ray

# What's in RtD right now?
rtd-redirects list

# Dump the live state to YAML.
rtd-redirects dump --output doc/redirects/current.yaml

# Edit the YAML, then dry-check the diff.
rtd-redirects plan --file doc/redirects/current.yaml

# Apply (interactive — confirms before mutating).
rtd-redirects apply --file doc/redirects/current.yaml
```

## Subcommands

### `list`

Print every redirect currently configured on the RtD project.

```bash
rtd-redirects list --project anyscale-ray
```

### `dump`

Export the RtD project's redirects to a YAML file (or stdout if `--output` is omitted).

```bash
rtd-redirects dump --project anyscale-ray --output doc/redirects/current.yaml
```

Output is collapsed: records sharing every field except `from_url` are written as a single multi-source entry with `from:` as a list.

### `plan`

Compute the diff between your YAML and the RtD project. No mutation.

```bash
rtd-redirects plan --project anyscale-ray --file doc/redirects/current.yaml
```

The output uses `+` for adds, `-` for deletes, `~` for updates, `@` for position-only reorders, plus a footer counting each phase.

### `diff-file`

Compute the redirect-level diff between two git refs of a YAML file. No RtD API calls — runs entirely from `git show`. This is the PR-time check engine.

```bash
rtd-redirects diff-file --file doc/redirects/current.yaml \
    --base origin/master --head HEAD
```

### `apply`

Apply the YAML to RtD. Confirms interactively unless `--yes` is set.

```bash
# Interactive
rtd-redirects apply --project anyscale-ray --file doc/redirects/current.yaml

# Non-interactive (CI)
rtd-redirects apply --project anyscale-ray --file doc/redirects/current.yaml --yes
```

Operations run in order: deletes → adds → updates → reorders. Each emits a single audit line to stderr.

### `audit`

Report drift between your YAML and the RtD project. Exits non-zero on drift so CI can surface it.

```bash
rtd-redirects audit --project anyscale-ray --file doc/redirects/current.yaml
```

## YAML schema

### Minimal

```yaml
schema_version: 1
redirects:
  - from: /en/latest/old.html
    to:   /en/latest/new.html
    type: exact
```

### Multi-source

One destination, several sources. Each source becomes its own RtD redirect record.

```yaml
schema_version: 1
redirects:
  - from:
      - /en/latest/old1.html
      - /en/latest/old2.html
    to: /en/latest/new.html
    type: exact
```

### Multi-version with defaults

Fan a single rename across the active version set. Path-only URLs get qualified with `/<language_prefix>/<version>` per version.

```yaml
schema_version: 1
defaults:
  versions: [latest, master]
redirects:
  - from: /rllib/rllib-algorithms.html
    to:   /rllib/algorithms.html
    type: exact
```

Expands to four RtD records: `/en/latest/rllib/rllib-algorithms.html`, `/en/master/rllib/rllib-algorithms.html`, both pointing at their version-matched `/en/<v>/rllib/algorithms.html`.

### Per-entry `versions:` override

```yaml
schema_version: 1
defaults:
  versions: [latest, master]
redirects:
  - from: /data/old.html
    to:   /data/new.html
    type: exact
    versions: [latest]   # override: only redirect on latest, not master
```

### Cross-product (sources × versions)

```yaml
schema_version: 1
redirects:
  - from:
      - /old1.html
      - /old2.html
    to: /new.html
    type: exact
    versions: [latest, master]
```

Expands to four records: latest×{old1, old2} and master×{old1, old2}.

### Cross-host destination

`to:` can be any absolute URL — useful for redirecting legacy docs to `docs.anyscale.com` or blog posts.

```yaml
schema_version: 1
redirects:
  - from: /en/latest/old.html
    to:   https://docs.anyscale.com/new-thing
    type: exact
```

`from:` must always be a project path. RtD only intercepts requests for paths it serves; external `from` URLs are rejected at parse time.

### Wildcards (`*` and `:splat`)

RtD supports a single suffix wildcard `*` in `from_url`, with `:splat` in `to_url` substituting the matched portion. Prefix and infix wildcards are not supported by RtD.

```yaml
schema_version: 1
redirects:
  # Bulk redirect every page under one prefix to the same path under another.
  - from: /en/releases-2.40.0/*
    to:   /en/latest/:splat
    type: exact

  # Combine with version expansion: one rule × N versions.
  - from: /rllib/rllib/*
    to:   /rllib/:splat
    type: exact
    versions: [latest, master]
```

The tool is a string passthrough for URL fields — `*` and `:splat` are stored as-is and interpreted by RtD at request time. Useful for the cohort cutover (legacy version slug → current) and prefix-collapse renames.

### `page` redirects apply across all versions automatically

A `page` redirect with `from: /old.html, to: /new.html` triggers on `/en/latest/old.html`, `/en/master/old.html`, every legacy version — **RtD handles the fan-out itself**. Don't pair `page` with `versions:` or `defaults.versions`; the tool ignores `defaults.versions` for `page` entries, and an explicit `versions:` raises a parse error.

```yaml
schema_version: 1
defaults:
  versions: [latest, master]   # applies only to `exact` entries below
redirects:
  - from: /old.html             # page: ignores defaults.versions
    to:   /new.html
    type: page
  - from: /api.html             # exact: fans out to latest and master
    to:   /api-v2.html
    type: exact
```

Same applies to `clean_url_to_html` and `html_to_clean_url` — these describe project-wide URL transitions and don't need `from:` or `to:` at all.

```yaml
schema_version: 1
redirects:
  - type: html_to_clean_url     # turn /page.html into /page/
```

### Rule ordering: specific before general

RtD picks the first redirect whose `from` matches the request URL — **position-based first-match, not specificity-based**. To make a specific rule override a catch-all wildcard, give the specific rule a lower `position` (or just write it earlier in the YAML; `position` defaults to entry index).

```yaml
schema_version: 1
redirects:
  # Specific override fires first (position 0).
  - from: /en/releases-2.40.0/api/special_case.html
    to:   /en/latest/api/its_new_home.html
    type: exact

  # Catch-all wildcard fires for everything else under that version (position 1).
  - from: /en/releases-2.40.0/*
    to:   /en/latest/:splat
    type: exact
```

The tool preserves ordering across `dump` / `parse` / `apply`. `diff` flags position-only changes as `reorder` and runs them in a separate pass at apply time so positions settle without churning the data phase.

### Inactive versions and slug renames

When you mark a version inactive on RtD, its artifacts are deleted and its URLs start returning 404. Combined with `force: false` (redirects fire on 404), this means **deactivating a version automatically routes its URLs through any matching redirect rule**. Your wildcard catch-all picks up all the old paths without any extra work.

Renaming a version slug has the same effect — old-slug URLs return 404, and matching wildcard rules fire. RtD's own docs suggest pairing slug renames with an `exact` wildcard:

```yaml
# After renaming releases-2.40.0 -> v2.40.0:
- from: /en/releases-2.40.0/*
  to:   /en/v2.40.0/:splat
  type: exact
```

(There's a [known corner case](https://github.com/readthedocs/readthedocs.org/issues/9335) where an inactive version's HTML can linger in storage and produce an infinite-redirect loop. RtD's infinite-redirect detector returns 404 as a failsafe; worth knowing about if you see one in practice.)

### Avoid chained redirects

RtD doesn't promise to resolve chains server-side. If `/a → /b` and `/b → /c` are both configured, RtD serves two 3xx responses (the browser follows each hop). Write each `from` pointing **directly at the final destination** rather than relying on the chain to collapse. If you renamed `/old → /intermediate → /current` over time, the final rule should be `/old → /current` (rewrite the existing redirect, don't stack).

If RtD detects an infinite loop, it returns 404 and stops trying — useful failsafe, but not a substitute for clean authoring.

### Robust fan-out: `page` + `force: false` + `*`/`:splat`

RtD's redirect rules default to `force: false`, which means **a redirect only fires when the source URL would otherwise 404**. Combined with `page` (applies across all versions) and a suffix wildcard, you get a single rule that does the right thing on every version without having to enumerate which versions it applies to.

Concrete example — auto-generated API module renamed from `old_module` to `new_module` in current docs, but the old name still exists in legacy version archives that you don't want to rebuild:

```yaml
schema_version: 1
redirects:
  - from: /api/old_module/*
    to:   /api/new_module/:splat
    type: page
    # force defaults to false: redirect fires only where /api/old_module/... 404s.
```

What happens at request time:

| Version | `/api/old_module/foo.html` exists? | Behavior |
|---|---|---|
| `latest` (after rename) | no | redirect fires → `/api/new_module/foo.html` |
| `v2.55` (rename hasn't happened) | yes | no redirect, original page renders |
| `releases-2.40.0` (legacy) | yes | no redirect, frozen archive intact |

One rule, applied semantically — newer versions get the redirect, older versions keep working. Authoring this with `force: true` or per-version `exact` rules would break legacy renders or require N rules across versions.

Use `force: true` only when you specifically want to override an existing page — e.g., taking over a path that still exists in current docs but should now point elsewhere. Default `force: false` is almost always what you want for IA cleanup.

### Custom language prefix

The URL language segment is configurable per file. Default is `/en`.

```yaml
schema_version: 1
language_prefix: /de
defaults:
  versions: [latest]
redirects:
  - from: /alt.html
    to:   /neu.html
    type: exact
```

Languageless RtD setups (no language segment) are not yet supported — see [`AGENTS.md`](AGENTS.md) for the deferred-work catalog.

### Field reference

| YAML field | RtD field | Default | Notes |
|---|---|---|---|
| `schema_version` | n/a | required | Top-level. Currently `1`. |
| `language_prefix` | n/a | `/en` | Top-level. URL segment between host and version. |
| `defaults.versions` | n/a | unset | Active version list for entries that inherit. |
| `from` | `from_url` | required for `page` and `exact` | String or list. Must be a project path, not external. Optional for `clean_url_to_html` / `html_to_clean_url`. |
| `to` | `to_url` | required for `page` and `exact` | String. Path-only, fully-qualified, or external (`https://`, `mailto:`, etc.). Optional for `clean_url_to_html` / `html_to_clean_url`. |
| `type` | `type` | required | One of `page`, `exact`, `clean_url_to_html`, `html_to_clean_url`. Only `exact` uses `versions:` / `defaults.versions`; the others apply project-wide on RtD's side. |
| `versions` | n/a (expansion input) | falls back to `defaults.versions` | List of plain version names. Pattern identifiers (globs, ranges, exclusions, macros) are not yet supported. Only valid on `type: exact`. |
| `status` | `http_status` | `301` | 3xx code. |
| `force` | `force` | `false` | |
| `enabled` | `enabled` | `true` | |
| `description` | `description` | `""` | Operator notes. Surface in PR diff output. |
| `position` | `position` | entry index | Set explicitly only when ordering matters. |

## Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `RTD_API_TOKEN` | required | Your RtD v3 API token. Never written to disk by the tool; never logged. Read from a secret store at the start of the shell session. |
| `RTD_PROJECT_SLUG` | optional | Alternative to `--project` flag. |
| `RTD_BASE_URL` | optional | API base. Defaults to `https://readthedocs.com/api/v3` (Business). Set to `https://readthedocs.org/api/v3` for Community. |

## Development

```bash
git clone git@github.com:anyscale/rtd-redirects.git
cd rtd-redirects
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
pytest                       # 253 tests
ruff check .                 # lint
```

### Branch naming

`doc-XXX-short-description` per the Anyscale docs team convention (where `DOC-XXX` is the Jira ticket key).

### PR conventions

- Reference `[DOC-XXX]` in the title or summary.
- Include a test plan in the body.
- Add or update tests for any module change.
- Run `ruff check .` and `pytest` locally before pushing.

For broader project intent, architecture, and the deferred-work roadmap, see [`AGENTS.md`](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
