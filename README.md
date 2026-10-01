# rtd-redirects

Manage [Read the Docs](https://readthedocs.com/) redirects as code. A YAML file in your docs repo is the source of truth; this CLI reconciles it against the [RtD v3 API](https://docs.readthedocs.com/platform/latest/api/v3.html).

## Status

v0.1.0 in development. Built for `docs.ray.io` IA-cleanup campaigns; the patterns generalize to any RtD project. Design rationale and full architecture are in [`anyscale/docs:strategy/ray-docs/redirect-mgmt/`](https://github.com/anyscale/docs/blob/master/strategy/ray-docs/redirect-mgmt/).

## Why

Read the Docs has no bulk redirect import. Its dashboard UI requires clicking through each entry by hand, which makes any meaningful slug-rename or IA-cleanup campaign untenable. `rtd-redirects` reads a YAML file from your repo, diffs it against the live RtD state, and applies the diff. PR-time mode produces a git-only diff with no API calls. Merge-time mode applies via API.

## Install

```bash
python -m pip install anyscale-rtd-redirects
```

The PyPI distribution is `anyscale-rtd-redirects`. The installed CLI command is
still `rtd-redirects`.

For local development, install the package in editable mode:

```bash
git clone git@github.com:anyscale/rtd-redirects.git
cd rtd-redirects
python -m pip install -e .[dev]
```

## Release

The package publishes to PyPI as `anyscale-rtd-redirects`. The command-line
entry point remains `rtd-redirects`.

Releases are tag-driven. The version comes from the git tag through
`setuptools-scm`, so the tag is the only thing you bump. There's no version
string to edit in the source. Pushing a `v*` tag triggers `publish.yml`, which
runs the tests, builds the distribution, and publishes to PyPI through a Trusted
Publisher (OIDC, no stored token).

The PyPI Trusted Publisher is already configured with these values:

| Field | Value |
|---|---|
| PyPI project | `anyscale-rtd-redirects` |
| Owner | `anyscale` |
| Repository | `rtd-redirects` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

To cut a release, complete the following steps. Replace `vX.Y.Z` with the next
version, for example `v0.2.0`.

1. Verify the test and package checks locally:

   ```bash
   python -m pip install --upgrade -e .[dev] build twine
   ruff check .
   pytest
   python -m build
   python -m twine check dist/*
   ```

1. Create and push a version tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

1. Confirm the `publish` GitHub Actions workflow succeeds.

1. Verify the published package from a clean environment:

   ```bash
   python -m pip install anyscale-rtd-redirects==X.Y.Z
   rtd-redirects --help
   ```

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

Report drift between your YAML and the RtD project, plus ordering / chain validation findings. Exits non-zero on either drift or validation errors so CI can surface them.

```bash
rtd-redirects audit --project anyscale-ray --file doc/redirects/current.yaml
```

### `validate`

Validate ordering and chain risks in one or more YAML files. **No RtD credentials required** — intended for local use by agents authoring redirects and for pre-commit hooks.

```bash
# Single file
rtd-redirects validate doc/redirects/current.yaml

# Several files, each validated on its own (pre-commit passes them this way)
rtd-redirects validate doc/redirects/*.yaml

# Auto-fix ordering errors in place (chains are left for the author)
rtd-redirects validate doc/redirects/current.yaml --fix

# List the per-rule detail for chain notes (info), otherwise summarized
rtd-redirects validate doc/redirects/current.yaml --show-info
```

Findings come in three severities:

- **`ERROR ordering`** — rule A's match set is a strict subset of rule B's, but A's position is higher. B fires first; A is unreachable. Lower A's position so it comes before B. `--fix` reorders deterministically. Errors fail `--strict` and the pre-commit hook.
- **`WARNING chain`** — rule A's `to` matches rule B's `from`, and B is `force: true`. B fires even where A's target exists, so the request chains on every version: a 3xx to A.to, then another to B. Point A's `to` at B's destination. Not auto-fixed.
- **`INFO chain`** — rule A's `to` could match rule B's `from`, but B is `force: false`, so it fires only where A's target 404s. The request chains only on versions without that page. After a move in newer versions only, that's the expected shape: the existing rule keeps older versions resolving. See [Avoid chained redirects](#avoid-chained-redirects). Rules alone can't show which versions have which pages, so run [`simulate`](#simulate) to check where each URL lands. A `:splat` move that a lower-position rule preempts from ever reaching B is also `info`, since it can't chain at all. Info findings are summarized as a count; `--show-info` lists them. They never fail `--strict`.

Validation is rules-based and decidable in closed form because RtD's pattern surface is intentionally narrow (suffix `*` only, four redirect types, no embedded wildcards). URL-style types (`clean_url_to_html` / `html_to_clean_url`) are excluded since they have no `from` URL to compare.

#### Pre-commit integration

This repo ships a [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml). Add to your project's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/anyscale/rtd-redirects
    rev: v0.1.0   # pin to a tag once one is published
    hooks:
      - id: rtd-redirects-validate
        files: ^doc/redirects/.*\.ya?ml$
```

The hook fails the commit on any `ERROR` finding. Run `pre-commit run rtd-redirects-validate --all-files` locally to surface issues before pushing.

`rtd-redirects-validate` validates each file independently.

#### `--strict` on `plan` / `apply`

`validate` is also wired into the project-bound commands for CI use:

```bash
# Dry-check ordering during plan
rtd-redirects plan --project anyscale-ray --file doc/redirects/current.yaml --strict

# Refuse to apply if ordering errors exist
rtd-redirects apply --project anyscale-ray --file doc/redirects/current.yaml --strict --yes
```

`audit` runs the validator unconditionally and exits non-zero if either drift or validation errors exist (drift is exit 1, validation error is exit 6; validation takes precedence).

#### Auto-fix caveats

`--fix` rewrites the YAML using the parsed `RedirectSet`, which loses comments and authoring formatting (`schema_version` and `language_prefix` are preserved). Run `--fix`, review the diff, and commit. The reordering is deterministic — sorted by `(specificity, original position, from_url, type)` — so re-running on a clean file is a no-op.

### `simulate`

Replay URLs through the redirect rules before and after a change, on every version in a matrix, and report where each one lands. **No RtD credentials required.**

`validate` checks a rule set on its own. A page-rename change can pass it with no errors and still send readers to a landing page, a 404, or a loop, because `force: false` rules fire only where a page is missing, and which pages are missing differs by version. `simulate` answers the question a rename PR needs answered: for every old URL, where does it land after this change, in how many hops, and is that the same page it reached before?

```bash
rtd-redirects simulate --file doc/redirects/current.yaml --base origin/master --head HEAD \
    --pages-before master=inv:https://docs.ray.io/en/master/objects.inv \
    --pages-after master=html:doc/_build/html \
    --pages latest=inv:https://docs.ray.io/en/latest/objects.inv \
    --pages releases-2.40.0=inv:https://docs.ray.io/en/releases-2.40.0/objects.inv \
    --prefix /ray-core/ --git-renames doc/source
```

**Rules.** `--file` reads the redirect file at `--base` for the before side and `--head` for the after side. `--head WORKTREE` reads the file on disk. `--before-file` and `--after-file` take on-disk files directly instead.

**Version matrix.** Each version needs a page set before and after the change. `--pages VERSION=SOURCE` sets both sides; `--pages-before` and `--pages-after` override one side. A version the change doesn't touch, such as an older release, uses `--pages` alone. Include `master`, `latest`, and at least one older release: a rule repointed at a moved page's new path is right on `master` and breaks on every version that doesn't have the move, so a matrix without older releases can't see it. `simulate` prints a note when the matrix has fewer than three versions.

A page set source is one of the following:

| Source | Reads | Notes |
|---|---|---|
| `html:DIR` | Every `*.html` file in a built HTML directory. | The most faithful source for a local build. |
| `inv:PATH_OR_URL` | Every `std:doc` entry in a Sphinx `objects.inv`. | Includes build-generated pages. RtD serves one per version, so it's the cheapest faithful source for a published version. |
| `sitemap:PATH_OR_URL` | Every `<loc>` in a `sitemap.xml`, with the host and any `/<lang>/<version>` prefix stripped. | |
| `git:REF:SRCDIR` | Each `.md`, `.rst`, or `.ipynb` file under `SRCDIR` at `REF`, mapped to `.html`. `REF` may be `WORKTREE`. | Cheap, but misses generated pages such as API stubs, which then read as 404s. |
| `list:PATH` | One path per line. | |

Compare like with like on each version. A git tree has no generated pages, so if one side is `git:` and the other a build, such as `inv:` or `html:`, every generated page looks added or removed by the change. `simulate` prints a note when that happens.

**Test URLs.** By default `simulate` tests every rule's `from` URL on both sides, with wildcards instantiated by a sample page name. `--prefix PATH` adds every page under `PATH` in any version's before set; for a directory move, pass the old directory. `--url` and `--urls-file` add explicit URLs. A `/<lang>/<version>/` URL runs only on that version; a plain path runs on every version. `--no-rule-sources` turns off the default set.

**Judging.** For each URL and version, `simulate` resolves both sides and assigns a verdict:

| Verdict | Meaning | Fails? |
|---|---|---|
| `regression` | Resolved before, doesn't after: a 404, a loop, or neither. | yes |
| `wrong-landing` | Resolves on both sides, but after lands somewhere other than the expected page. | yes |
| `new-loop` | Unresolved before, loops after. | yes |
| `unmapped` | Resolves on both sides, but the before landing no longer exists and no rename says where it went. | no |
| `fixed` | Unresolved before, resolves after. | no |
| `unresolved` | Unresolved on both sides. | no |
| `unverified` | A redirect jumps to a version outside the matrix. | no |
| `ok` | Lands on the expected page. | no |

The expected page is the before landing if it still exists after the change. Otherwise it's that landing mapped through the renames. `--git-renames SRCDIR` derives renames from git renames of Sphinx sources between `--base` and `--head`. `--rename-map FILE` takes a YAML map of old to new page paths, where a key ending in `*` maps a prefix with `:splat`:

```yaml
/ray-core/*: /core/:splat
/core/actors.html: /core/actors/index.html
```

Renames compose, so the two entries above send `/ray-core/actors.html` to `/core/actors/index.html`. Comparison is on the landing version, so a rule that jumps from `/en/latest/` to an explicit `/en/master/` target is judged against `master`'s pages.

Chains grow with each successive move, because existing rules keep their targets so older versions still resolve. `--hop-budget N` fails any URL whose after resolution needs more than `N` redirects. The report always lists URLs that need two or more. `--max-hops` (default 10) sets when a chain counts as a loop.

Output is a text report per version, or `--format json` for the per-version counts plus every outcome that isn't a single-hop `ok`. The exit code is 6 when any URL fails, matching `validate`.

The resolver models the semantics in [Wildcards](#wildcards--and-splat), [Rule ordering](#rule-ordering-specific-before-general), and [Robust fan-out](#robust-fan-out-page--force-false--splat): position-based first match, `force: false` firing only on a 404, `page` rules matching the path after `/<lang>/<version>`, `exact` rules matching the full path, a suffix `*` with `:splat`, fragments never matching server-side, and `/<lang>/<version>/` targets switching to that version's pages. Disabled rules and the URL-style types are ignored.

## YAML schema

A redirect set is one YAML file.

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

  # The same move on every version, firing only where the old path 404s.
  - from: /rllib/rllib/*
    to:   /rllib/:splat
    type: page
```

The tool is a string passthrough for URL fields — `*` and `:splat` are stored as-is and interpreted by RtD at request time. Useful for the cohort cutover (legacy version slug → current) and prefix-collapse renames.

### `page` redirects apply across all versions automatically

A `page` redirect with `from: /old.html, to: /new.html` triggers on `/en/latest/old.html`, `/en/master/old.html`, every legacy version — **RtD handles the fan-out itself**. To target specific versions instead, write one `exact` rule per version with a fully-qualified `from`:

```yaml
schema_version: 1
redirects:
  - from: /old.html                  # page: every version
    to:   /new.html
    type: page
  - from: /en/latest/api.html        # exact: latest only
    to:   /en/latest/api-v2.html
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

RtD doesn't promise to resolve chains server-side. If `/a → /b` and `/b → /c` are both configured, RtD serves two 3xx responses (the browser follows each hop). When you add a rule, point its `to` **directly at the final destination** rather than at another rule's `from`.

On a versioned project, don't flatten an existing rule when its destination later moves. Say `/old → /intermediate` exists, and a newer version renames `/intermediate` to `/current`. Older versions still serve `/intermediate`, so the existing rule resolves directly there. Add `/intermediate → /current` for the newer versions and leave `/old → /intermediate` alone. Readers of newer versions take two hops, and readers of older versions still land on a page. Rewriting the existing rule to `/old → /current` sends readers of every version without `/current` to a 404. Flatten only when the final destination exists in every version where the rule fires, such as on an unversioned project. The validator reports the kept chain as an `INFO chain`. Run [`simulate`](#simulate) to confirm it lands on a page on every version.

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

### Removed in 0.3.0

Two features built for designs that didn't ship were removed in 0.3.0. A file that still uses them fails to parse with an error that says how to rewrite it.

- **Multi-version expansion.** Top-level `defaults.versions` and per-entry `versions:` fanned a path-only `exact` rule out across versions. Use a version-less `page` rule instead, which RtD applies on every version and which fires only where the page 404s. See [Robust fan-out](#robust-fan-out-page--force-false--splat). When a rule must target specific versions, write one `exact` entry per version with a fully-qualified `from`, such as `/en/latest/old.html`.
- **Multi-file composition.** `plan`, `apply`, `audit`, `diff-file`, and `simulate` take a single `--file`. `validate --composed` and the `rtd-redirects-validate-composed` pre-commit hook are gone. Merge the files into one, keeping the earlier file's rules first. `validate` still accepts several files and checks each on its own.

The top-level `language_prefix:` key still parses so existing files keep working, but nothing reads it now that expansion is gone.

### Field reference

| YAML field | RtD field | Default | Notes |
|---|---|---|---|
| `schema_version` | n/a | required | Top-level. Currently `1`. |
| `language_prefix` | n/a | `/en` | Top-level. Accepted for compatibility; unused since 0.3.0. |
| `from` | `from_url` | required for `page` and `exact` | String or list. Must be a project path, not external. Optional for `clean_url_to_html` / `html_to_clean_url`. |
| `to` | `to_url` | required for `page` and `exact` | String. Path-only, fully-qualified, or external (`https://`, `mailto:`, etc.). Optional for `clean_url_to_html` / `html_to_clean_url`. |
| `type` | `type` | required | One of `page`, `exact`, `clean_url_to_html`, `html_to_clean_url`. `exact` matches the full `/<lang>/<version>/...` path; the others apply on every version. |
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
