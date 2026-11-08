# DB Copilot

Ask questions about a connected SQL Server database and receive the generated T-SQL plus its actual results.

`Question â†’ schema RAG candidates â†’ essential-schema selection â†’ SQL generation â†’ validation â†’ SQL + results`

## Run locally

Requires Python 3.12+, SQL Server, and Microsoft ODBC Driver 18. Copy `.env.example` to `.env` only when configuring a new checkout; keep an existing `.env`. Set your API key, model, connection URL, schema and restricted database user.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. API documentation: http://127.0.0.1:8000/docs.

The existing local connection uses `lpc:.\SQLEXPRESS`, database `DBCopilot`, Windows Authentication, and schema `dbo`. SQL Server must be running. `lpc:` uses local shared memory. `TrustServerCertificate=yes` is for this local instance's self-signed development certificate; configure a trusted certificate for a remote connection.

The app connects to an existing database. It creates no tables or records and has no built-in demo queries or domain-specific business rules. Add business definitions to SQL Server table descriptions (`MS_Description`), which are retrieved with the schema.

## Query permissions

`MSSQL_READONLY_USER` must identify an existing restricted database user. Each physical connection switches to that user using `EXECUTE AS USER ... WITH NO REVERT` before schema inspection or generated queries. The connecting Windows identity needs permission to impersonate that user. The existing local setup uses `copilot_reader`, which already has SELECT permissions on the approved tables and explicit write/EXECUTE denials. Changing databases requires provisioning an equivalent restricted user separately.

Driver pooling is disabled and SQLAlchemy uses fresh connections. Startup checks reject a user with table write permissions or database CONTROL/CREATE TABLE permissions. The local Windows identity remains privileged, so a deployed app should use a dedicated Windows identity with narrowly scoped access. The SQL parser separately rejects writes, multiple statements, system/cross-database tables, table functions, locking hints and unapproved functions. SQL Server permissions can still expose some public system metadata.

`ALLOWED_TABLES` optionally restricts tables exposed to RAG. Query timeout is rounded up to whole seconds by pyodbc; lock timeout and result row limits also apply.

## Schema RAG

The app extracts tables, columns, descriptions, primary keys and foreign keys. It embeds schema documents with `EMBEDDING_MODEL`, ranks tables by cosine similarity, retrieves `SCHEMA_TOP_K` candidates, connects them through foreign-key paths, and recursively includes parent tables. A separate structured model call then selects the tables and columns needed for output, filtering, grouping, ordering and aggregation. The API key needs Write access to `/v1/embeddings` and `/v1/responses`.

The selector lists only necessary foreign-key edges. The application verifies each selected identifier and relationship against the candidates, then adds both sides of those join keys (including composite keys and intermediate tables). It does not expand every parent again after selection. Independent aggregates can keep separate tables without unnecessary joins. Invalid selections fail without restoring the broad schema; ambiguous or unsupported requests ask for clarification and skip SQL generation.

Only the selected structural schema goes to SQL generation. SQL is validated against the selected columns as well as the database allowlist. Wildcard projections such as `SELECT *` and `alias.*` are rejected to prevent restoring omitted physical columns; `COUNT(*)` is allowed. Descriptions remain available for business meaning and may mention omitted entities, but SQL cannot reference their omitted columns or tables. The UI shows the final selected columns and the candidate-to-selected counts. The selection stage adds one model call per question before SQL generation.

Vectors are cached atomically in ignored `data/schema-index/`. A fingerprint of schema metadata, embedding model, dialect and namespace invalidates stale caches after restart. This is an exact-search local vector index. Documents are capped at 6,000 UTF-8 bytes per table for embedding; selection receives complete candidate metadata. Up to 128 question vectors are cached in memory per process. Retrieval does not persist question text. Schema metadata and questions are sent to OpenAI; database rows are not.

The index builds lazily on the first question. Provider failures produce an error without executing SQL. Restart to re-extract a changed schema. Selection aims for the smallest sufficient context, but it is model-based and does not mathematically guarantee a minimal or complete schema for every question. Candidate recall still limits what selection can find. Similarity scores are rankings, not correctness probabilities. Evaluate coverage, precision and SQL-result correctness separately; fewer columns alone is not proof of accuracy.

## API

- `GET /api/schema`: database name, extracted metadata, row limit and retrieval status.
- `POST /api/ask`: accepts `{"question":"your question"}` and returns `sql`, `columns`, `rows`, `row_count`, `truncated`, `explanation`, `error`, `elapsed_ms` and `retrieval`.
- `POST /api/schema/select`: accepts the same question and returns only the selected `schema`, `retrieval` trace and optional `clarification`. It generates no SQL and executes no data query; it does make embedding/selection calls when needed.

The retrieval trace distinguishes `candidate_tables` from final `context_tables`, lists `selected_columns`, and reports candidate/selected table and column counts. Candidate matches retain their similarity scores; excluded matches are not part of the SQL-generation schema.

`sql` is the exact normalized statement submitted for execution. Rows are arrays aligned to `columns`. At most `MAX_ROWS` rows are returned, with truncation detected using one additional fetched row. A clarification has `sql: null`. Validation failures return HTTP 422; provider/execution failures return HTTP 502. Generated SQL remains visible when available. Driver/provider details are redacted from user-facing errors.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Offline tests use isolated schema fixtures and mocked services. They cover retrieval, cache invalidation, precise table/column selection, composite join keys, independent aggregates, wildcard rejection, clarification, T-SQL validation and API responses without creating a demo database or making paid requests.

Opt-in SQL Server integration tests use the configured existing database and make no OpenAI requests:

```powershell
$env:RUN_SQLSERVER_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q
```

These checks cover reflection, SELECT execution, restricted identity, zero-row write rejection, API results, result limits and timeouts. They do not depend on commerce tables or fixed record counts. This local prototype has no authentication or multi-user isolation; run on loopback. Conversation memory and query repair are not implemented.
