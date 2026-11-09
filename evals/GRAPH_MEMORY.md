# Graph memory: running the evaluated configurations

This page covers the settings and commands for MIRIX with the provenance hypergraph. The
flat-memory instructions in [README.md](README.md) still apply; everything below is in
addition to them.

## Requirements

- PostgreSQL with `pgvector`, as for flat MIRIX.
- Neo4j 5.23 or later. The graph queries use `CALL (a, row) { ... }`, which older servers
  reject. Set your own `MIRIX_NEO4J_PASSWORD` before running
  `docker compose --profile graph up -d`, which starts Neo4j 5.26 with APOC.
  For a new local database, generate a password with `openssl rand -hex 32` and
  store it in your local `.env` or environment. For a hosted database, obtain your
  own credentials from its provider. No shared database password is included.
- Your own LLM and embedding API keys, obtained from the relevant providers and
  configured locally. Never commit credentials or `.env` files.
- The Python `neo4j` driver. It is in `requirements.txt`; after `pip install -e .` also run
  `pip install "neo4j>=5.20,<6"`.
- A database created before graph memory existed needs
  `scripts/migrate_add_graph_provenance_columns.sql` once. The server stops at startup and
  names the script if it is missing.

## Server settings

Graph memory has a single switch. With `MIRIX_ENABLE_GRAPH_MEMORY=true` the server
indexes episodic and semantic records into the hypergraph and retrieves them through it;
with `false` (the default) it behaves as the original flat MIRIX.

| variable | effect |
|---|---|
| `MIRIX_ENABLE_GRAPH_MEMORY` | `true` turns the hypergraph on; `false` keeps flat Postgres memory |
| `MIRIX_GRAPH_RERANK` | `1` (default) reranks graph candidates; `0` turns reranking off for ablations |
| `MIRIX_NEO4J_URI`, `MIRIX_NEO4J_USER`, `MIRIX_NEO4J_PASSWORD` | Neo4j connection (URI defaults to `bolt://localhost:7687`, user to `neo4j`; password required for graph operations, with no default) |
| `MIRIX_NEO4J_VECTOR_DIM` | dimension of the anchor-name index, 1536 by default; it must match the embedding model |

Start the server from the repository root with these variables set:

```bash
python scripts/start_server.py --port 8531
```

The evaluation clients connect to `http://127.0.0.1:8531`.

Evaluation tools launched directly from the shell, including `memory_snapshot.py`,
read the `MIRIX_NEO4J_*` environment variables; export the same connection settings
used by the server before running them. To stop using a previously shared password
on an existing database, change it in Neo4j first and update the environment to
match; changing `NEO4J_AUTH` alone does not rotate an existing database password.

## The three configurations

| configuration | `MIRIX_ENABLE_GRAPH_MEMORY` (server) | `MIRIX_DREAM_EVERY_N_CHUNKS` (eval) |
|---|---|---|
| M0, flat memory without AutoDream | `false` | `0` |
| M1, graph without AutoDream | `true` | `0` |
| M2, graph with AutoDream | `true` | `5` |

`MIRIX_DREAM_EVERY_N_CHUNKS` is read by the evaluation script, which triggers one AutoDream
cycle every N ingested chunks and after the last one (`MIRIX_DREAM_MODE` defaults to
`experience`). The evaluation scripts use the same question-answering protocol in all three
configurations.

## LoCoMo

Put `locomo10.json` under `evals/data/`, then from `evals/`:

```bash
unset MIRIX_COLDFACT MIRIX_HYBRID_SEARCH MIRIX_SEARCH_LIMIT   # experiment flags; leave unset
export MIRIX_EVAL_USER_PREFIX=m2__                            # namespaces this run's users
export MIRIX_DREAM_EVERY_N_CHUNKS=5                           # M2; use 0 for M0 and M1
python main_eval.py --data data/locomo10.json --run-llm \
    --mirix_config_path ./configs/0201c.yaml --output_path m2
python organize_results.py --judge default results/locomo/m2
```

Give every run its own `MIRIX_EVAL_USER_PREFIX`; the Neo4j graph is shared across runs.

**Answering again without re-ingesting.** A change that does not alter what is written can be
measured on an existing store. Seed a new output directory from a finished run with its
answers cleared; `main_eval.py` then sees the chunk markers and only answers:

```python
import json, pathlib
src = pathlib.Path("results/locomo/m2")
dst = pathlib.Path("results/locomo/m2_qa_only"); dst.mkdir(parents=True, exist_ok=True)
for f in src.glob("conv-*.json"):
    if f.name.endswith("_memories.json"):
        continue
    d = json.loads(f.read_text())
    d["records"] = {}
    (dst / f.name).write_text(json.dumps(d, ensure_ascii=False, indent=2))
```

**Before scoring a run**, check that it wrote where it should:

```bash
python assert_store_sane.py --db <postgres-db> --prefix m2__ --graph   # PSQL=/path/to/psql if not on PATH
```

## MemoryAgentBench

The four tracks run through the scripts in `mab/` with `configs/mab.yaml`; each script
lists its prerequisites and options at the top.

| track | command |
|---|---|
| LongMemEval-S | `LIMIT=5 ./mab/run_mab_longmem_eval.sh` (300 questions; it also snapshots the store with `memory_snapshot.py`) |
| SHDocQA | `python mab/longmem_eval.py --limit 1 --max-chunks 0 --run-llm --source ruler_qa1_197K --output_path shdoc --mirix_config_path ./configs/mab.yaml` |
| MHDocQA | `python mab/longmem_eval.py --limit 1 --max-chunks 0 --run-llm --source ruler_qa2_421K --output_path mhdoc --mirix_config_path ./configs/mab.yaml` |
| DetectiveQA | `python mab/lru_eval.py --limit 10 --max-chunks 0 --run-llm --source detective_qa --output_path detective --mirix_config_path ./configs/mab.yaml` |
