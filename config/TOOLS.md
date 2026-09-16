# VARIANT-1 tools

This file is a human reference. The provider sees one `ipython` schema. Runtime
capability names, descriptions, and arguments are published through the
preloaded `tools` and service globals inside that environment.

IPython is trusted same-user host Python, not a sandbox. Ordinary Python
imports, filesystem access, subprocesses, and networking are available with
the VARIANT-1 process's authority. Category mounts govern only host-integrated
VARIANT-1 proxies; they do not restrict standard Python. The current project is the
starting directory, not an access boundary. Follow explicit user-requested
scope or isolated-environment boundaries.

There are no SAFE/CONFIRM/CRITICAL tiers, point-of-action approval cards,
autonomy modes, file allowlists, shell filters, browser public-host policy, or
model-action approval ledger. Schema validation, cancellation, timeouts,
output bounds, invocation receipts, and activity diagnostics remain operational
contracts.

## File spine

- `glob`: list a folder or find files by name.
- `grep`: search inside file contents.
- `read_file`: read a known text file.
- `apply_patch`: write, replace exact text in, or delete text files. Use
  `changes=[...]` for a batch, `change={...}` for one operation, or direct
  `path` plus `content`/`find`/`replace`; all forms enter one transaction.
- `run_command`: shell work such as move, copy, mkdir, tests, and builds.
  Nonzero exits return structured diagnostics by default; use `check=True` only
  when raising is useful.

Select the `build` category to mount these capabilities. The provider schema
does not change as categories are mounted.

Slot-owned domain objects are thin entry points. The direct-seed namespace also
supports `tools.methods()` (the same names as `tools.aliases()`). Mount cards
carry entry signatures; use `object.methods()` and `object.describe("method")`
only when another contract detail is needed. Artifact, Child, Job, and
Connector continuation belongs on the bound
handle returned by the root; do not copy IDs or leases back into equivalent
root methods.

## Web and browser

- `web_search(query)` searches current public sources; when `query` is a complete
  HTTP(S) URL, it reads that page instead.
- Multi-step source analysis is composed by the model in IPython with
  `web_search`, Browser handles, ordinary Python, project files, and
  artifacts.
- `browser` owns navigate, read, screenshot, click, and fill for VARIANT-1's
  interactive browser session. Returned handles keep compact continuation:
  session pages/history/tracing, page observe/navigation/keys/wait/evaluate,
  and element actions. This is separate from the public web-search primitive.

## Desktop

- Launch applications, local files, folders, or visible URLs with ordinary
  same-user Python or `run_command`; no separate launch seed or application
  allowlist exists.
- `computer` is the one desktop object. Select a returned window with
  `computer.list_windows()` and `computer.focus(...)`, refresh it with
  `computer.observe(...)`, then pass the returned view to one action method.
- Actions return the refreshed view. Window activation, input routing, modal
  transitions, evidence capture, locking, and receipts are handled by the host.

## Skills and integrations

Skills expose a short catalog and load their full instructions only when used.
Connected MCP tools, resources, prompts, and immutable plugin packages are
discovered with `connectors.search(...)`. Its primary MCP handle is returned
beside its exact schema and can be invoked in the same IPython cell; secondary
or stale handles use `schema()`. Set `conclude=True` only on a final
authoritative invocation. User-side MCP setup,
reconnect, refresh, disconnect, and removal use the single `mcp-v2` Deck
transport; there is no legacy `mcp:*` control path.
