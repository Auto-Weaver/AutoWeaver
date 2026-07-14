# AutoWeaver

Reactive, event-driven framework for industrial vision inspection systems
(`autoweaver` 0.10). Kernel lives in `src/autoweaver/`: camera → pipeline →
tasks → worker, with `frames` / `servo` / `motion_policy` / `reactive`
subsystems. Keep the kernel's design clean — downstream work belongs in a
translation layer, not in the kernel.

## Python

Use **uv** for all Python commands (`uv run`, `uv pip`, `uv sync`).
Do not call `pip` or `python` directly.

## Reading the code — default to codegraph

This repo is indexed by **codegraph** (`.codegraph/`, MCP server `codegraph`).
It is a pre-built symbol/edge index — reads are sub-millisecond and it returns
verbatim source. **Use it FIRST** to read, trace, and explore code, before
reaching for Read / Grep / Glob:

- "how does X work", architecture, a trace, where-is-X, surveying an area
  → `codegraph_explore` (one call returns the relevant symbols' source grouped
  by file — usually the only call needed)
- locate a symbol by name → `codegraph_search`
- what calls this / what it calls / blast radius of a change
  → `codegraph_callers` / `codegraph_callees` / `codegraph_impact`
- one symbol's full body, or an overloaded name → `codegraph_node`
- list / inspect indexed files → `codegraph_files`

Consult codegraph **before** writing or editing, not during — the watcher lags
writes by ~1s. Fall back to Read/Grep only to confirm a detail codegraph didn't
cover, or for things it doesn't index (raw configs, docs, generated files).
