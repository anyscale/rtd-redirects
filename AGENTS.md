# AGENTS.md

Contributor and agent rules for `rtd-redirects`. Captures what this project is, how it's structured, the conventions for changes, and the explicit list of deferred work so nothing gets lost between sessions.

## Project intent

A small Python CLI that drives [Read the Docs](https://readthedocs.com/) redirects from a YAML file in a docs repo. The YAML is the source of truth; the tool reconciles it against the [RtD v3 API](https://docs.readthedocs.com/platform/latest/api/v3.html).

The load-bearing use case is the IA-cleanup and version-renaming campaigns on `docs.ray.io`. Read the Docs has no bulk redirect import: the dashboard UI requires clicking through each entry by hand, which makes any 500–2000-entry campaign untenable. `rtd-redirects` closes that gap.

Anchor docs (in the `anyscale/docs` repo, not this one):

- [`strategy/ray-docs/redirect-mgmt/prd.md`](https://github.com/anyscale/docs/blob/master/strategy/ray-docs/redirect-mgmt/prd.md) — product framing, problem, requirements, resolved decisions.
- [`strategy/ray-docs/redirect-mgmt/design.md`](https://github.com/anyscale/docs/blob/master/strategy/ray-docs/redirect-mgmt/design.md) — implementation architecture, rollout phasing, open questions.
- [DOC-851](https://anyscale1.atlassian.net/browse/DOC-851) — implementation ticket.
- [DOC-844](https://anyscale1.atlassian.net/browse/DOC-844) — umbrella epic for OSS Ray docs investment.

## OSS posture

Public repo, MIT-licensed, Anyscale-opinionated (PRD Option B). The tool ships publicly and any RtD project can adopt it, but the default ergonomics (301 status, `/en` language prefix, multi-source / multi-version expansion, `/en/<version>/` URL detection) reflect Ray's setup. Bug reports welcome; generalization work is best-effort.

Revisit to "Option A" (full OSS, actively generalized) only if outside adoption interest materializes.

## Architecture

The MVP is ~1000 LOC across nine modules. Each is documented at the top of the file.

| Module | Responsibility |
|---|---|
| `model.py` | `Redirect` dataclass + `RedirectSet`. Identity is `(from_url, type)`. |
| `exceptions.py` | `ParseError` — shared between `parse` and `expand` to avoid a circular import. |
| `client.py` | `RtdClient`. Token-bucket rate limit (60 rpm), pagination, 429 retry with `Retry-After`, CRUD on `/projects/<slug>/redirects/`, list on `/projects/<slug>/versions/`. |
| `parse.py` | YAML reader. Schema validation. Routes expansion-shaped entries to `expand`. Exposes `parse_file`, `parse_files`, `parse_text`. |
| `expand.py` | Multi-source and multi-version fan-out. Path-only-vs-fully-qualified detection. Configurable `language_prefix`. |
| `collapse.py` | Dump-time inverse of `expand`. Groups canonical records into ergonomic YAML entries. Tier 1 only — see Deferred work below. |
| `diff.py` | `Diff` of two `RedirectSet`s. Categories: adds / updates / deletes / reorders. |
| `diff_file.py` | Git-only PR-time diff. Reads YAML at two refs via `git show`, runs each through `parse_text`, returns a `Diff`. No API. |
| `apply.py` | Drives a `Diff` against `RtdClient` in safe order: deletes → adds → updates → reorders. Per-entry stderr audit log. |
| `validate.py` | Rules-based ordering and chain detection over a `RedirectSet`. Flags unreachable rules (specific-with-higher-position-than-general) and chain candidates (A.to matches B.from). |
| `cli.py` | `argparse` entry point. Wires seven subcommands: `list`, `dump`, `plan`, `diff-file`, `apply`, `audit`, `validate`. Validation runs always on `audit` and `validate`, and on `plan` / `apply` with `--strict`. |

## Key design choices

- **Identity is `(from_url, type)`**, not the API `pk`. Same data identifies the same record whether it came from YAML or RtD.
- **`pk` is excluded from `Redirect.__eq__`** (via `field(compare=False)`). YAML-parsed records (no `pk`) compare cleanly against API-fetched records (`pk` set).
- **External `from` URLs are rejected** at parse time. RtD can only intercept requests for paths it serves. External `to` URLs are fully supported (cross-host redirects to `docs.anyscale.com`, blog posts, `mailto:`, etc.).
- **`language_prefix` is configurable** per YAML file. Hard-coded `/en/` is not assumed anywhere except as a default.
- **`apply` runs in safe order**: deletes free identities; adds create; updates settle data; reorders fix positions last so the position counter doesn't churn during data changes.
- **Reorders are mutually exclusive with updates** — a position-plus-other-field change is an update (one PUT sets both); a position-only change is a reorder.

## Redirect types (current RtD API, verified May 2026)

RtD's current v3 API supports exactly four redirect types. Our `model.REDIRECT_TYPES` matches.

| Type | `from`/`to` required? | Version semantics | Use case |
|---|---|---|---|
| `page` | yes | **applies across all versions automatically** (`VERSION_AGNOSTIC_TYPES`) | path rename that should hit every version RtD serves |
| `exact` | yes | per-URL match including version segment | path rename scoped to specific version(s); the IA-cleanup workhorse |
| `clean_url_to_html` | no (`URL_STYLE_TYPES`) | project-wide URL transition | `/page/` → `/page.html` style switch |
| `html_to_clean_url` | no (`URL_STYLE_TYPES`) | project-wide URL transition | `/page.html` → `/page/` style switch |

**Critical consequence**: only `type: exact` uses `versions:` / `defaults.versions`. `page` and the URL-style types skip our expansion logic entirely — RtD's API handles fan-out across versions on its side. Mixing `page` and `exact` under one `defaults.versions` is the natural authoring pattern; the tool routes each through the correct path automatically.

**Wildcards**: `*` is allowed only as a *suffix* in `from_url`. `:splat` in `to_url` substitutes the matched portion. The tool is a string passthrough for these — they're stored verbatim and interpreted by RtD at request time.

**Ordering**: RtD applies the first redirect (by `position`) whose `from` matches the request — strict first-match, not specificity-based. To make a specific rule override a catch-all wildcard, the specific rule must have a lower `position`. Our `dump` / `parse` / `apply` pipeline preserves position byte-for-byte; `diff.py` flags position-only changes as `reorder` and runs them in a final pass.

**Inactive versions and slug renames**: deactivating a version on RtD deletes its artifacts and serves 404 for its URLs. Slug renames have the same effect on old-slug URLs. Because `force: false` is the default and redirects fire on 404, both events automatically route the affected URLs through any matching wildcard or page redirect. This is a feature, not a bug — designers can defer "what happens to legacy version URLs" until they're ready to deactivate.

**Chains**: RtD doesn't promise server-side chain resolution. If `/a → /b` and `/b → /c` are configured, the browser follows both 3xx responses. Author each `from` pointing at the *final* `to`. RtD's infinite-redirect detector returns 404 as a failsafe but isn't a substitute for clean authoring. Today's `validate.py` flags chain candidates as warnings.

### Local validation and pre-commit

Agents authoring redirects (and humans editing them) should reach for `rtd-redirects validate <file>` first — it runs the validator without needing project credentials or API access. The hook surface for `pre-commit` lives at the repo root (`.pre-commit-hooks.yaml`) so consumer projects can wire `id: rtd-redirects-validate` into their `.pre-commit-config.yaml`.

`--fix` reorders deterministically by `(specificity, original_position, from_url, type)`. The rewrite is lossy on comments and authoring formatting but byte-stable on the canonical record set; re-running on a clean file is a no-op. Chains are not auto-fixed — they require choosing the right destination and that's an authoring decision.

### Preferred pattern for IA-cleanup redirects: `page` + `force: false` + `*`/`:splat`

RtD's `force` field defaults to `false`, which means **a redirect fires only when the source URL would otherwise 404**. Combined with `page` (applies across all versions on RtD's side) and a suffix wildcard, this yields a single rule that automatically does the right thing across every version:

- On versions where the source still exists (legacy archives), the redirect is silent and the original page renders.
- On versions where the source was moved or deleted (the version that motivated the rename), the redirect fires.

Agents authoring redirects should reach for this pattern first. It avoids enumerating versions, doesn't need to be re-evaluated when new versions are cut, and preserves legacy correctness automatically. Example: `from: /api/old_module/*, to: /api/new_module/:splat, type: page` (force omitted, defaults to false).

Reserve `exact` redirects for cases where you specifically need version-targeted behavior — e.g., a legacy-cohort cutover that should redirect *only* on the legacy versions. Reserve `force: true` for cases where you want to take over an existing path.

**Removed from older designs**: `prefix`, `sphinx_html`, and `sphinx_htmldir` (legacy type names in `design.md`). RtD's current API doesn't accept these — wildcards replaced `prefix`, and `clean_url_to_html` / `html_to_clean_url` replaced the Sphinx-builder transitions.

## Conventions

### Branch and PR

- Branch names: `doc-XXX-short-description` per Anyscale docs team convention. `DOC-XXX` is the Jira ticket key.
- PRs reference `[DOC-XXX]` in title or summary.
- Include a test plan checklist in the PR body.
- One module per PR (or one focused refactor). Land via squash merge so `main` stays linear.

### Lint and style

- Python 3.10+ minimum.
- `ruff check .` is the only linter; default config in `pyproject.toml` (`E`, `F`, `W`, `I`, `B`, `UP`).
- Max 100 chars per line (set in `pyproject.toml`).
- `src/` layout. Package code under `src/rtd_redirects/`; tests under `tests/`.
- Module-level docstrings explain *intent* (what / why), not call-site recipes.
- Inline comments only when the *why* would surprise a reader. Code-as-doc otherwise.

### Tests

- Coverage target: 90% per design.md. Current: 97% overall.
- Unit tests use `unittest.mock` (no `responses` / `requests-mock` dep). `RtdClient` is mocked via `MagicMock(spec=RtdClient)`.
- Integration tests for `diff_file` use real git repos in `tmp_path`.
- Token-bucket and time-related tests monkeypatch `time.sleep`.
- Run locally before pushing: `ruff check . && pytest`.

### Commits

- Lowercase, imperative, scoped subject: `cli: wire all six subcommands end-to-end [DOC-851]`.
- Body explains the *why* and notable design choices.
- Trailing `Co-Authored-By: Claude Opus 4.7 (1M context)` line when AI-assisted.

## Deferred work

Captured here so it doesn't get lost. Listed in rough priority order.

### Operational follow-ups (no code work in this repo)

1. **First real `dump` against `anyscale-ray`** — bootstrap the source-of-truth YAML for `ray-project/ray/doc/redirects/current.yaml`. Tracked under [DOC-928](https://anyscale1.atlassian.net/browse/DOC-928).
1. **PyPI release of `0.1.0`** — publish so `pip install rtd-redirects` works without a clone. Set up a GitHub Actions workflow that builds and publishes on tag.
1. **Buildkite integration (phase 2)** — PR-time `diff-file` step and merge-time `apply` step in `ray-project/ray/.buildkite/`. Path-filtered to `doc/redirects/**` so it adds no work to Ray's existing test graph. Lives in the Ray repo, not this one.
1. **Bot-user provisioning handoff** — `anyscale-ray-docs-ops` RtD user provisioned by REEf + IT. Switch from personal admin token to bot-user token in 1Password and Buildkite secrets once landed.

### Feature gaps in the tool

1. **Multi-version collapse (Tier 2 in `collapse.py`)** — currently `collapse` only does multi-source grouping. Tier 2 detects records that differ only in their language/version prefix and factors them into `versions:` lists. Useful once IA-cleanup PRs have shipped real expanded records to fold back. Validate against a real `dump` after the first IA pass.
1. **Pattern version identifiers in `expand.py`** — glob (`v2.5*`), semver range (`>=v2.50`), exclusion (`!v2.54`), macro (`@active`). Currently raise "not yet supported". Adding requires:
   - Live version list resolution: thread `RtdClient.list_versions` (already implemented) through to `expand_entry` via a `version_resolver: Callable[[], list[str]] | None` parameter.
   - Pattern parsing per the table in `redirect-mgmt/design.md` §"Multi-version".
   - Caching: design says "for the duration of a single command invocation".
1. **Languageless URL prefix (`language_prefix=""`)** — rejected today. Supporting it needs path-only-vs-fully-qualified detection without a language segment. Options:
   - Require explicit `known_versions:` list at YAML top level.
   - Infer from `defaults.versions` + per-entry `versions:`.
   - Use live version list (same lift as the pattern-identifier feature).
   Pre-requisite if `docs.ray.io` ever drops `/en`. Note: the *migration* from `/en/...` to `/...` can be done today with a single suffix-wildcard exact redirect (`/en/*` → `/:splat`); the deferred work is ongoing YAML authoring AFTER the prefix is gone.
1. **Wildcard `*` placement validation** — RtD only accepts suffix wildcards. We pass URL strings through without checking; an infix or prefix `*` (e.g. `/foo/*/bar`) would be rejected by the API at apply time with a clearer error than we could give. Could add a parse-time check, but the cost/benefit is marginal — agents writing redirects rarely make this mistake, and RtD's error is informative.
1. **Validator follow-ups**. The first cut of `validate.py` covers ordering (specific-must-come-first) and chain candidates (A.to overlaps B.from). Future work that builds on the same Pattern machinery:
   - **Cycle detection** — A.to matches B.from, B.to matches A.from. Today this surfaces as two separate chain findings; a cycle-aware pass could flag the loop explicitly so the operator sees it as one finding instead of N.
   - **Wildcard `*` placement** — RtD rejects prefix and infix wildcards. Today the API returns the error at apply time; the validator could catch it at parse time with a clearer message.
   - **Splat-substitution precision** — chain detection treats `:splat` conservatively (literal-prefix match). A more precise model would resolve the actual substituted URL against B's pattern; trade-off is more code for fewer false positives.
   - **Multiple language prefixes** — when a project hosts multiple language variants, the validator should accept a list of language prefixes or resolve them from the YAML's per-file `language_prefix:`. Today it takes a single prefix.
1. **Source-file + line tracking on `Redirect`** — design.md asks `apply` to log per-entry with "source YAML file and line number". Currently `apply` only logs the URL. Adding requires `source_file: Path | None` and `source_line: int | None` fields on `Redirect` (with `compare=False`), populated by `parse` and `expand`, surfaced by `apply` log lines.
1. **Flask integration test fixture** — design.md mentions a `pytest` fixture with a Flask server that models the v3 API. Currently we only have unit tests with mocks. Worth adding once a real apply hits an edge the mocks didn't cover.
1. **`apply --no-delete`** — design.md mentions a flag that skips destructive operations and applies only adds/updates. Useful for cautious first runs.
1. **`audit --reconcile`** — design.md mentions a mode that writes drift back into the YAML source file using the `collapse` heuristic on just the drift set, with a `# backfilled by audit --reconcile on YYYY-MM-DD` marker comment.
1. **Multi-file YAML support via CLI** — `parse_files` already handles multiple YAML files with sorted concatenation, but the CLI subcommands (`plan`, `apply`, `audit`) only accept a single `--file`. Extend to accept a directory or glob.
1. **`list-versions` subcommand** — surface `RtdClient.list_versions` to the CLI for debugging.
1. **Structured (JSON) output for `plan` / `diff-file`** — design.md mentions "plain-text and JSON diff output" as a goal. Add a `--format json` flag.

### Open questions from PRD/design

These resolve during the rollout, not as code changes here:

1. **RtD version-switcher behavior for renamed paths** — design.md §"Trade-offs the team should understand". Test during the week-3 backfill: when a user on `/en/latest/<renamed>` switches to a legacy version where that path doesn't exist, does RtD's version-switcher fall back gracefully? If not, file upstream or document the constraint.
1. **Buildkite fork-PR policy** — need a Buildkite-specific answer for how fork PRs are handled for the redirect pipeline. The proposed step-level scoping keeps the apply step away from PR events entirely; verify that's sufficient.
1. **Final tool/package name** — `rtd-redirects` is the working name. Confirmed indefinite through MVP; re-validate before wiring into Ray's Buildkite.

## What lives where (anti-confusion)

- **This repo (`anyscale/rtd-redirects`)** — the tool. Code, tests, packaging, this AGENTS.md.
- **`anyscale/docs:strategy/ray-docs/redirect-mgmt/`** — the strategy and design. PRD, design doc, rollout plan, resolved decisions. Read this before making non-trivial design changes.
- **`ray-project/ray/doc/redirects/`** — the YAML source of truth for `anyscale-ray`. Eventually contains `current.yaml` (post-backfill) plus the Buildkite pipeline integration.
- **DOC Jira project** — ticket tracking for the docs team's work. Each PR references `[DOC-XXX]`.
