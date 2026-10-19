# Contributing to Filament Agent Kit

For a new provider guide, follow [Add an agent guide](docs/adding-a-guide.md).

Guides describe the current setup and behavior. Keep them concise. Let the agent
compare the guide with its existing setup; do not add a changelog or instructions
for migrating through individual versions.

## Guide versions and PR review

- All guides are versioned. `guides.json` maps each guide ID to its directory
  (`path`) and Markdown filename (`guide`). The inventory key is the stable
  guide/provider ID for release tooling; new IDs need no code registration.
  Any change in that directory,
  including tests and supporting documentation, requires a version bump.
- The guide's frontmatter is the only source of its `guide_id` and `version`.
  Use `YYYY-MM-DD.N`, with a UTC date and a positive daily sequence number.
- When a PR changes a registered guide or any of its covered files, bump that
  guide before opening the PR. One bump covers the PR's changes; do not bump for
  each review commit. Recheck against the current base before merging.
- Edit the version directly in the guide's frontmatter. On the same UTC date,
  increment N; on a later date, start at 1. Never move the version backwards.
  If another PR used the same version, bump again before merging.
- Reviewers check the affected guides, their version bumps and their coverage
  in `guides.json`. Removing coverage must not hide a change from version review.
- Run `python3 -m unittest discover -s tests -v` and
  `python3 scripts/guides.py --base origin/main` before proposing a PR.
  Missing or invalid versions and missing bumps fail the check. It blocks a
  merge only when configured as required in the repository's GitHub rules.
  The script only checks files; it does not bump, commit or release anything.

## Releases

Merging a guide publishes its content. Each Git commit identifies an exact
snapshot of the kit, while each guide has its own version. This repository's
checks do not declare releases or notify existing agents.

Release preparation and operation belong in the Filament CLI, with their
documentation alongside that tooling. The CLI integration is a separate
follow-up; it is not required to version guides or run this repository's checks.
