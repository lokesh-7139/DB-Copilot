# DB Copilot

Ask a question about SQL Server and compare two independently generated answers.

- **With RAG:** semantic table retrieval and relationship expansion, followed by one SQL-generation LLM call using every column of those tables.
- **Without RAG:** the complete accessible catalog and the question go directly to a separate SQL-generation request. No retrieval or selection is performed.

The browser sends two independent POST requests per question. Each answer tab keeps its own SQL, results, schema context, timing, loading state and error. Routine success explanations are hidden; clarification questions remain visible. Failure in one branch does not prevent the other from completing. Results are not passed between branches or sent to OpenAI. Matching answers do not establish correctness.

## Local setup

Requires Python 3.12+, SQL Server, and Microsoft ODBC Driver 18.

1. Create a virtual environment and install `requirements.txt`.
2. For a new checkout, copy `.env.example` to `.env` and configure your API key, model and SQL Server connection. Preserve an existing `.env`.
3. As a database administrator, run `scripts/grant_read_access.sql` in the target database. The script creates or updates `copilot_reader`, grants database-wide SELECT and VIEW DEFINITION, and denies INSERT, UPDATE, DELETE and EXECUTE. If using a different `MSSQL_READONLY_USER`, adjust the script's user name.
4. Start the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. One process serves the website and API. The configured Windows identity must have permission to impersonate the restricted reader. Local `lpc:.\SQLEXPRESS` uses shared memory; the example trusts the local development certificate.

## Schema discovery and refresh

The application discovers every visible user table across all user schemas in the connected database. System schemas are excluded. Tables are identified by qualified names such as `dbo.customers` and `sales.customers`; cross-schema foreign keys retain the correct target. Unqualified table names are accepted only when they resolve uniquely and are executed with an explicit schema. Discovery is limited by the database reader's permissions; explicit SELECT denials still apply.

`DB_SCHEMA` and `ALLOWED_TABLES` are no longer used. Old values in an existing `.env` are ignored. Database-wide reader grants cover new user schemas and tables unless SQL Server permissions explicitly restrict them.

Schema is checked when serving schema/question requests, with a five-second metadata cache. The **Refresh** button forces an immediate refresh without restarting the backend. Changed tables, columns and relationships replace the catalog and retriever together; in-flight requests keep their own consistent snapshot. Cached embeddings are reused when the catalog is unchanged, and rebuilt lazily when its fingerprint changes. Merely changing data does not require refreshing schema.

## Execution and configuration

Both branches use the same model, generation instructions, missing-requirements check, SQL validation, restricted database identity, timeout and row limit. The RAG branch sends the complete metadata of retrieved tables and their relationship tables to a single SQL-generation call. No LLM schema-selection call or column pruning is performed. Missing requirements produce a clarification instead of a guessed query.

Every connection uses `EXECUTE AS USER ... WITH NO REVERT`; pooling is disabled. Metadata and permission checks have a 30-second statement budget. Generated queries use `QUERY_TIMEOUT_MS` rounded up to whole seconds by the driver, plus the configured lock timeout. At most `MAX_ROWS` rows are returned. Only a single validated read-only SELECT is executed. SQL validation rejects writes, SELECT INTO, multiple statements, table functions, unapproved functions, locking hints, unavailable tables/columns and cross-database access. This app runs locally without multi-user authentication; bind it to loopback.

- `DATABASE_URL`, `MSSQL_READONLY_USER`: database connection and restricted reader.
- `OPENAI_API_KEY`, `OPENAI_MODEL`: provider credentials/model, used independently by both branches.
- `EMBEDDING_MODEL`, `SCHEMA_TOP_K`: RAG embedding model and initial match count, not a final table-count limit.
- `MAX_ROWS`, `QUERY_TIMEOUT_MS`: execution limits shared by both branches.

RAG vectors are stored in ignored `data/schema-index/`. Question embeddings are cached in memory only. Table embedding documents are capped at 6,000 UTF-8 bytes; the without-RAG branch sends complete metadata without truncating it. Large full catalogs can exceed the model's context limit; that branch then reports an error rather than silently switching to RAG. Comparing both modes makes additional paid API requests.

## API

- `GET /api/schema`: database name, current qualified catalog, retrieval status and row limit.
- `POST /api/schema/refresh`: force refresh and return the updated catalog.
- `POST /api/ask`: JSON `{"question":"your question","mode":"rag"}` or `"mode":"full"`. Mode defaults to `rag` for existing clients. Each request independently generates, validates and executes its own SQL.
- `POST /api/schema/select`: inspect retrieved tables and their complete columns without SQL generation or data execution. This legacy endpoint name is retained for compatibility; it makes embedding requests only when needed, and no LLM selection call.

Answers contain `mode`, `sql`, `columns`, `rows`, `row_count`, `truncated`, `explanation`, `error`, `elapsed_ms` and `retrieval`. Generated SQL is the statement actually submitted for execution. Validation failures return HTTP 422; provider/execution failures return HTTP 502. Credentials and driver/provider details are not returned to the browser. Schema metadata and questions are sent to OpenAI; result rows remain local.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Offline tests make no paid requests and cover retrieval, complete schema contexts, single-call generation, independent comparison branches, refresh, qualified identifiers and SQL validation.

To include read-only SQL Server integration checks against the configured database:

```powershell
$env:RUN_SQLSERVER_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q
```

These check discovery, actual results, restricted identity, write denial, result caps and timeouts. They do not seed or delete business data. Conversation memory and general SQL repair are not implemented.

## Resource comparison

After both independent requests finish, a right-aligned **Comparison** tab appears. It is hidden before the first response pair completes and resets for every new question. It compares end-to-end time, schemas/tables/columns sent to SQL generation, schema payload size, model input/output/total tokens, embedding tokens, call counts and embedding-cache hits. Expand the schema lists to inspect the actual table names supplied to each stage.

The `resources` field in each answer contains request-local `calls`, `cache_hits`, `llm_calls`, `embedding_calls`, `input_tokens`, `output_tokens`, `total_tokens` and `embedding_tokens`. Each branch uses one SQL-generation LLM call; RAG embedding usage is separate. There are no schema-selection tokens or calls. A warm question/schema embedding cache makes no embedding API call and adds zero new embedding tokens. Missing API usage stays null (displayed as Unavailable), while failed calls can leave partial measurements. Call counts refer to application SDK calls and do not itemize SDK retries. Counts are not a billing statement.

Schema payload size measures the serialized schema metadata sent across LLM calls, for SQL generation, and excludes questions, instructions and embedding inputs. Time includes schema refresh, retrieval, generation, validation and execution. Output tokens include API-reported reasoning/non-visible tokens. These metrics show the per-question tradeoff rather than claiming RAG is always faster or cheaper. See the [OpenAI token usage guide](https://developers.openai.com/api/docs/guides/token-counting).

RAG flow: Question → retrieve relevant tables → add relationship tables → retain all columns → one SQL-generation LLM call → validate → execute → return SQL and results. `SCHEMA_TOP_K` controls the initial ranked table count, not the expanded context size. Related tables can increase that size, and some questions may retrieve the entire catalog; savings are measured rather than assumed.
