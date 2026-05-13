# rtd-redirects

Manage [Read the Docs](https://readthedocs.com/) redirects as code. A YAML file in your docs repo is the source of truth; this CLI reconciles it against the RtD v3 API.

## Status

Pre-alpha. v0.1.0 in development. See the [design notes](https://github.com/anyscale/docs/blob/master/strategy/ray-docs/redirect-mgmt/design.md) for scope and rollout plan.

## Why

Read the Docs has no bulk redirect import. Its dashboard UI requires clicking through each entry by hand, which makes any meaningful slug-rename or IA-cleanup campaign untenable at scale.

`rtd-redirects` reads a YAML file from your repo, diffs it against the live RtD state via the [v3 API](https://docs.readthedocs.com/platform/latest/api/v3.html), and applies the diff. PR-time mode produces a git-only diff with no API calls. Merge-time mode applies via API.

## Install

```bash
pip install rtd-redirects
```

## Usage

```bash
export RTD_API_TOKEN=...  # user token, never commit, never log

rtd-redirects dump   --project anyscale-ray --output redirects.yaml
rtd-redirects plan   --project anyscale-ray --file redirects.yaml
rtd-redirects apply  --project anyscale-ray --file redirects.yaml
rtd-redirects audit  --project anyscale-ray --file redirects.yaml
```

Run `rtd-redirects --help` for the full command set.

## YAML format

```yaml
schema_version: 1

defaults:
  versions: [latest, master]

redirects:
  - from: /rllib/rllib-algorithms.html
    to:   /rllib/algorithms.html
    type: exact
    description: "Drop redundant rllib- prefix"
```

All five RtD redirect types (`page`, `exact`, `prefix`, `sphinx_html`, `sphinx_htmldir`) are supported, plus multi-source and multi-version expansion. Schema documentation lands alongside the parser module.

## Development

```bash
git clone git@github.com:anyscale/rtd-redirects.git
cd rtd-redirects
pip install -e .[dev]
pytest
```

## License

MIT. See [LICENSE](LICENSE).
