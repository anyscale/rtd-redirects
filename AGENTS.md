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
| `cli.py` | `argparse` entry point. Wires the six subcommands: `list`, `dump`, `plan`, `diff-file`, `apply`, `audit`. |

## Key design choices

- **Identity is `(from_url, type)`**, not the API `pk`. Same data identifies the same record whether it came from YAML or RtD.
- **`pk` is excluded from `Redirect.__eq__`** (via `field(compare=False)`). YAML-parsed records (no `pk`) compare cleanly against API-fetched records (`pk` set).
- **External `from` URLs are rejected** at parse time. RtD can only intercept requests for paths it serves. External `to` URLs are fully supported (cross-host redirects to `docs.anyscale.com`, blog posts, `mailto:`, etc.).
- **`language_prefix` is configurable** per YAML file. Hard-coded `/en/` is not assumed anywhere except as a default.
- **`apply` runs in safe order**: deletes free identities; adds create; updates settle data; reorders fix positions last so the position counter doesn't churn during data changes.
- **Reorders are mutually exclusive with updates** — a position-plus-other-field change is an update (one PUT sets both); a position-only change is a reorder.

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
   Pre-requisite if `docs.ray.io` ever drops `/en`.
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
