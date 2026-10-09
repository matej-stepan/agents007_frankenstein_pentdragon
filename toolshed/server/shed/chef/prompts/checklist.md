## Comprehensive tool checklist (big tools; small tools where it applies)
1. Validate args; raise ValueError on bad input.
2. Use >= 2 sources, or one source with a fallback. Put every source URL in "sources".
3. Follow pagination up to a limit (an arg with a sane default). Use a page-2 URL form that a probe showed works.
4. Dedup results by a stable key (normalised URL or id).
5. Normalise units into explicit fields: an amount with an ISO 4217 currency code, <quantity>_<unit> names, ISO 8601 dates. The target currency and locale are args.
6. Return {"results": [...], "warnings": [...], "sources": [...]}. Never fail silently: add a warning.
7. Retries with backoff and a timeout on every request.
8. Rank results and give each a short "reason".
9. The core (parsing, normalising, ranking) works offline and is tested with mocks.
10. SKILL.md states the limits (sites covered, max pages, what is not handled).
11. Result URLs come from the source data. Build a URL from an id only with a pattern that a probe showed returns HTTP 200. Never guess a URL pattern.
