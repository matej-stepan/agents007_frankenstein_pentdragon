## manifest.json
- name: ^[a-z][a-z0-9_]{2,40}$, generic (not task-specific); new, except in an extend. grade: "big" (composes small tools, uses not empty) or "small" (generic, reusable).
- summary (<= 120 chars), description (precise: inputs, outputs, sources/URLs, edge cases; it is the only spec the test writer and coder get), keywords (<= 16).
- input_schema, output_schema: JSON Schema objects. Big tools output {"results": [...], "warnings": [...], "sources": [...]}.
- uses: tool names this tool may shed.call. deps: catalog package names (from pkg_search), [] for stdlib only.
- permissions: {"network": bool, "llm_usd": 0..0.25 (0 = no shed.llm), "files": "none"|"read"|"write" (/work)}.
- limits: {"timeout_s": 1..180 (default 60), "memory_mb": 128..4096}. examples: [{"args": {...}, "note": "..."}] (>= 1).
