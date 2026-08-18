# CLAUDE.md

## Agent guides (read before matching work)

**Before debugging or changing anything on the server:** read
[docs/agent-guides/server-ops.md](docs/agent-guides/server-ops.md) — restart
procedure (no sudo), and rules on secrets, verification, diagnosis-only mode.

**Before any deploy to `/opt/dailypost/app`:** read
[docs/agent-guides/deployment.md](docs/agent-guides/deployment.md) — it's not
a git repo, deploys are manual, server fixes must be mirrored back to git.

## Code exploration

Use the codegraph MCP tool instead of Grep/Glob for symbol/code-structure
lookups — it understands the AST (definitions, call chains) while Grep only
matches text. Load it via `ToolSearch` (`select:mcp__codegraph__codegraph_explore`)
if not already available. Grep/Glob are still fine for non-symbol text (env
vars, log strings, config keys) or plain file-path search.
