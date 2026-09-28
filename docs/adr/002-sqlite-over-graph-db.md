# ADR-002: SQLite over a Graph Database

## Status
Accepted

## Context
The index stores symbols (classes, methods, functions, fields), their locations, import relationships, and call-site references. Queries include: look up a symbol by name (fuzzy), find all callers of a symbol, list a file's outline, and trace module-level dependency edges. This data is naturally a graph (symbols reference other symbols, modules import modules).

## Decision
Use SQLite (Python stdlib) with FTS5 for full-text symbol search. Store relationships as rows in relational tables (symbols, references, imports) with foreign keys and indexes, not as edges in a graph database.

## Alternatives Considered

### Neo4j / Dgraph / other graph DB
Natural data model for references and dependencies. Cypher/GraphQL queries for transitive closure are concise. However:
- Requires running an external service — adds install complexity and breaks the "no external services" constraint.
- Overkill for the query patterns we actually need. Most queries are one-hop (direct callers, direct imports) or two-hop (callers of callers for diff impact).
- Harder to distribute as a single-file tool.

### In-memory dictionaries
Fast for small repos, zero dependencies. But no persistence means full reindex on every run — no incremental updates. Data structures are ad-hoc and harder to query flexibly.

## Consequences
- **Zero external dependencies:** SQLite is in Python's stdlib. The index is a single `.db` file.
- **FTS5 covers symbol search:** prefix matching and ranked results out of the box.
- **Incremental updates are straightforward:** check file content hashes, delete/reinsert only changed files.
- **Graph queries are possible but verbose:** one-hop joins are simple SQL. Multi-hop (transitive closure) requires recursive CTEs or application-level BFS. This is acceptable because most tool queries are one or two hops.
- **Portability:** the index file can be moved, copied, or deleted without side effects.
