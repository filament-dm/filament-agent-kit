# Official MCP Registry listing

`server.json` is Filament's entry in the [official MCP Registry](https://registry.modelcontextprotocol.io): the hosted agents API at `https://api.filament.dm/mcp/agents`, listed as `dm.filament/agents`. Smithery, Glama and PulseMCP pull from the registry, so this one listing feeds several directories.

The listing is remote-only (`remotes`, no `packages`), so no npm package and no repository field are needed. Clients find the OAuth flow from the endpoint's `401` / `WWW-Authenticate`, the same as any MCP client today.

## Namespace proof

`dm.filament/*` is proved by a DNS TXT record at the apex of `filament.dm` (not under a selector), holding the Ed25519 public key:

    filament.dm. IN TXT "v=MCPv1; k=ed25519; p=<public key>"

The matching private key is held by whoever publishes; it is not in this repo.

## Publishing

Versions are immutable once published, so every edit to `server.json` needs a new `version`.

    brew install mcp-publisher
    mcp-publisher validate mcp-registry/server.json
    MCP_PRIVATE_KEY=<64-char hex> mcp-publisher login dns --domain filament.dm
    mcp-publisher publish mcp-registry/server.json

To hide a version: `mcp-publisher status --status deleted dm.filament/agents <version>`.
