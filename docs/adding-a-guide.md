# Add an agent guide

The minimum catalog change is one Markdown guide and one entry in
`guides.json`. The inventory accepts new guide IDs as data; adding an ID does
not require editing the checker or introducing a provider enum. Add a README
link so people can discover the guide.

This registers instructions for a harness that can use an existing connection
flow. A guide does not itself implement a new authentication method, runtime
plugin or connection option in the app.

## 1. Choose a name and create the guide

For the standard public short URL, use a lowercase name starting with a letter,
with letters, digits and single hyphens between words, at most 40 characters.
Use that name consistently:

| Item | Example |
| --- | --- |
| Stable guide ID | `example` |
| Directory | `filament-example` |
| Guide file | `EXAMPLE.md` |
| Public short URL | `https://filament.dm/agents/example.md` |

For a name such as `example-agent`, the filename is `EXAMPLE-AGENT.md`.
The short URL follows this naming convention; it does not read `guides.json`.
The inventory allows other relative paths, but those do not automatically
receive a matching short URL. Prefer the convention for a new guide.

Create `filament-example/EXAMPLE.md` with this starting structure. Replace the
example date with today's UTC date and replace the bracketed descriptions with
instructions tested on the harness:

```markdown
---
guide_id: example
version: "2026-10-19.1"
---

# Connect an Example agent to Filament

## Connect

[Describe the supported connection flow and the credentials or authorization
it needs. Explain how the agent verifies its identity and confirms connection.]

## Receive and handle work

[Describe the harness's supported way to receive work, fetch relevant context
and answer. State how it recovers after a failure or a later run.]

## Keep the setup current

[Explain where persistent instructions live. When given an updated guide,
compare it with the existing setup and reconcile differences while preserving
identity, credentials, preferences and pending work.]
```

Write the current desired setup, without a changelog or instructions for
migrating through older versions. Include only capabilities and tools that
are available to this harness. Keep the guide self-contained enough to follow;
link external dependencies explicitly when a plugin or script is necessary.

## 2. Add one inventory entry

Add this entry to the existing object in `guides.json`, preserving its other
entries. Do not replace the inventory with this single-entry example:

```json
{
  "example": {
    "path": "filament-example",
    "guide": "EXAMPLE.md"
  }
}
```

The key must match the guide's `guide_id`. The version lives only in the
guide's frontmatter; do not duplicate it in the inventory. Each registered
directory belongs to one guide. Put its supporting files under that directory
so their changes participate in the version check.

Add a link to the README's harness guide list. This is discovery documentation,
not a second registration step for the tooling.

## 3. Check and review

From the repository root, with an up-to-date target branch reference:

```sh
python3 scripts/guides.py --base origin/main
python3 -m unittest discover -s tests -v
```

Follow the [versioning rules](../AGENTS.md#guide-versions-and-pr-review).
Test connection and work handling on the harness, then open a PR with the guide,
inventory entry, README link and test results.

## 4. Publish content and declare a version separately

After merge, verify that the published guide is readable at its intended URL.
The standard short URL follows the main branch and is not an immutable release
reference. For an exact published revision, use the repository's commit URL.

Version declaration and delivery to existing agents are separate operations.
Their tooling and operating instructions belong with the Filament CLI; see
[the release boundary](../AGENTS.md#releases). This repository's versioning
change does not depend on that integration being available.

## Review the amount of work

| Work | Why it exists |
| --- | --- |
| Write the guide | Describes the harness's actual connection and execution behavior |
| Add one inventory entry | Makes the guide discoverable to version tooling |
| Add a README link | Makes the guide discoverable to people; currently manual |
| Run the check and try the harness | Separates valid metadata from working instructions |
| Declare a published version later | Makes a reviewed version available to the release system without selecting recipients |
