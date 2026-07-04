# text2sql-secure

A production-style **Safe & Secure Text-to-SQL system** powered by a local
LLM. Natural language goes in, SQL comes out — but nothing touches your
database until it's been through a real security guard.

```
User → LLM → SQL → Validator → Safe Execution → Results
```

The LLM translates English into SQL. We never blindly trust it: every
generated query is independently validated (statement-type allow-list,
schema allow-list, keyword blocklist, stacked-query detection, row-limit
enforcement) and then executed against a **read-only** database connection
with a query timeout — regardless of what the model intended.

Runs entirely on your machine. No data or schema ever leaves it.

## Why local?

- **Privacy** — your schema and questions never hit a third-party API
- **Control** — you own the prompt, the validator, and the guardrails
- **Cost** — no per-token API bill
- **Security** — the whole attack surface is inspectable and testable

## Architecture

```
text2sql-secure/
│
├── src/
│   ├── core/            # config.py (layered settings), logger.py
│   ├── llm/
│   │   ├── ollama_client.py   # talks to a real local Ollama model
│   │   └── mock_client.py     # offline rule-based stand-in for demos/tests
│   ├── prompts/
│   │   └── templates.py       # schema-constrained prompt ("constrain the model")
│   ├── db/
│   │   ├── schema.py           # introspects the DB -> allow-list for prompt + validator
│   │   └── executor.py         # read-only execution, timeout, row cap
│   ├── security/
│   │   ├── validator.py        # THE security guard — see below
│   │   └── audit.py             # append-only JSONL audit trail
│   ├── services/
│   │   └── text2sql_service.py # orchestrates the full pipeline
│   ├── api/
│   │   └── routes.py            # /query, /health, /schema, /audit
│   └── main.py                   # wiring + FastAPI app
│
├── configs/config.yaml
├── scripts/
│   ├── setup_env.sh
│   └── seed_db.py                # creates data/sample.db (e-commerce schema)
├── tests/
│   ├── unit/            # validator, schema, executor
│   └── integration/     # full pipeline, blocked-attack scenarios
└── data/sample.db        # created by seed_db.py
```

## The security guard, in detail

Every candidate SQL string produced by the LLM passes through
`src/security/validator.py`, which enforces several **independent**
layers of defense — no single bypass is enough:

1. **Structural sanity** — must parse as exactly one SQL statement
   (via [`sqlglot`](https://github.com/tobymao/sqlglot)); stacked
   statements like `SELECT ...; DROP TABLE ...;` are rejected outright.
2. **Statement allow-list** — only `SELECT` is ever permitted. `INSERT`,
   `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `ATTACH`, `PRAGMA`,
   `TRUNCATE`, etc. are all rejected regardless of framing.
3. **Keyword blocklist** — a defense-in-depth regex pass over the raw
   text, catching dangerous keywords even outside the primary
   AST-based check.
4. **Schema allow-list** — every table referenced (including inside
   `JOIN`s) must exist in the schema explicitly exposed to the model.
   This is what stops queries against `sqlite_master` or any hidden
   table the LLM might hallucinate or be tricked into referencing.
5. **Row-limit enforcement** — a `LIMIT` is injected if missing, or
   clamped if the model asked for too much, so no single query can pull
   back an unbounded result set.

The validator returns a **re-serialized** SQL string (built from the
parsed AST, not the original text), so inline comments and other
textual tricks are dropped even if they somehow survived parsing.

On top of that, `src/db/executor.py` opens the SQLite connection in
**read-only mode** (`file:...?mode=ro`) and enforces a query timeout via
SQLite's progress handler — so even a bug in the validator can't result
in a write, and a runaway query can't hang the service.

Every attempt — successful, blocked, or errored — is written to
`logs/audit.jsonl` via `src/security/audit.py`, with the original
question, the raw LLM output, the sanitized SQL, and the outcome.

## Quickstart

```bash
./scripts/setup_env.sh
```

This installs dependencies (auto-detecting Poetry/UV/pip), creates
`.env` from `.env.example`, and seeds `data/sample.db` with a small
e-commerce dataset (customers, products, orders, order_items).

Run the API:

```bash
poetry run python src/main.py
# or: uvicorn src.main:app --reload
```

By default the app runs in **offline mock mode** (`llm_provider: mock`
in `configs/config.yaml`) — a small rule-based "model" that lets you
exercise the entire pipeline with zero setup. Ask it things like *"how
many customers do we have"*, *"top 3 products"*, or *"what is our total
revenue"*.

### Using a real local LLM (Ollama)

```bash
# 1. Install Ollama: https://ollama.com
ollama serve
ollama pull llama3.2

# 2. Point the app at it
# configs/config.yaml
llm_provider: "ollama"
ollama_model: "llama3.2"
```

Nothing else changes — `OllamaClient` and `MockLLMClient` share the same
`generate_sql(question, schema_text)` interface, so the rest of the
pipeline (validator, executor, audit) is completely unaffected by which
one is active.

## Try it

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "how many customers do we have"}'
# {"status": "success", "safe_sql": "SELECT COUNT(*) ... LIMIT 50", "rows": [[5]], ...}

curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "please drop the customers table"}'
# {"status": "blocked", "reason": "Query contains a forbidden keyword: 'drop'", ...}

curl http://localhost:8000/schema
curl "http://localhost:8000/audit?limit=10"
```

## Tests

```bash
pytest -v
```

The test suite includes an explicit set of attack scenarios the
validator must catch: `DROP`/`DELETE`/`UPDATE`/`INSERT`/`ALTER`/`CREATE`,
`ATTACH DATABASE`, `PRAGMA`, stacked queries, comment-smuggling, queries
against `sqlite_master` or any table outside the allowed schema, and
joins that try to pull in a disallowed table.

## Extending

- **New tables/columns**: just add them to the SQLite DB — `get_schema()`
  introspects at request time, so the prompt and validator's allow-list
  stay in sync automatically.
- **A different database engine** (Postgres, MySQL): swap
  `src/db/executor.py` and `src/db/schema.py`, using a genuinely
  read-only DB role/user in addition to the validator (defense in depth
  at the infrastructure layer, not just in application code).
- **A different local LLM runtime** (llama.cpp server, LM Studio, vLLM):
  implement a client with the same `generate_sql()` interface as
  `OllamaClient` and wire it up in `src/main.py::build_llm_client`.
