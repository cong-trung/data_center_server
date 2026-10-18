# Data Center Server

FastAPI service that receives CSV/Excel files, processes them with [Polars](https://pola.rs)
(optionally on an NVIDIA GPU) and returns the result. Nothing is stored: each file is processed
in memory and sent straight back to the client. It also runs the
[unit analysis](#unit-analysis) of test results, so that an LLM agent only has to summarize
them.

## Project structure

```text
app/
├── main.py              # App factory: lifespan, middleware, exception handlers, routers
├── api/
│   ├── main.py          # Mounts all routers under API_V1_STR (/api/v1)
│   ├── deps.py          # Shared dependencies: upload validation, processing limiter
│   ├── downloads.py     # File download responses (Content-Disposition)
│   └── routes/
│       ├── health.py    # GET  /api/v1/health
│       ├── files.py     # POST /api/v1/files/process, POST /api/v1/files/inspect
│       └── unit.py      # POST /api/v1/unit/analyze, POST /api/v1/unit/pivot
├── core/
│   ├── config.py        # Settings (environment variables / .env)
│   ├── engine.py        # Polars engine selection (GPU probe with CPU fallback)
│   ├── exceptions.py    # AppError hierarchy and JSON error handlers
│   ├── logging.py       # Logging setup
│   └── middleware.py    # Request body size limit (413)
├── schemas/             # Pydantic models and enums used by the API
└── services/
    ├── file_io.py       # CSV/Excel -> DataFrame, DataFrame -> CSV/XLSX/JSON
    ├── processing.py    # Your business logic: process_dataframe()
    └── unit_analysis.py # Unit analysis: validation, pivot, classification, Pareto
```

## Local development

Requires [uv](https://docs.astral.sh/uv/). The GPU engine also needs an NVIDIA GPU and driver.

```bash
uv sync                  # create .venv and install dependencies
cp .env.example .env     # optional: override settings
uv run fastapi dev       # http://127.0.0.1:8000 with auto-reload
```

Interactive API docs: <http://127.0.0.1:8000/docs>

## API

| Method | Path                        | Description                                                                                               |
| ------ | --------------------------- | --------------------------------------------------------------------------------------------------------- |
| GET    | `/api/v1/health`            | Status, version and the Polars engine in use                                                              |
| POST   | `/api/v1/files/process`     | Upload a file, download the processed result (`output_format=csv\|xlsx\|json`)                            |
| POST   | `/api/v1/files/inspect`     | Upload a file, get its row/column counts, column types, null counts and first rows                        |
| POST   | `/api/v1/unit/analyze`      | Upload test results, get the [unit analysis](#unit-analysis) as JSON                                      |
| POST   | `/api/v1/unit/analyze-json` | Same, with the file base64-encoded in a JSON body ([for AI agents](#json-endpoints-for-ai-agents))        |
| POST   | `/api/v1/unit/pivot`        | Upload test results, download the pivot with each unit's classification (`output_format=csv\|xlsx\|json`) |
| POST   | `/api/v1/unit/pivot-json`   | Same, with the file base64-encoded in a JSON body ([for AI agents](#json-endpoints-for-ai-agents))        |

Uploads are `multipart/form-data` with a `file` field: `.csv`, `.xlsx` or `.xls` (first sheet).
The `-json` endpoints take the file base64-encoded in a JSON body instead.

```bash
curl -OJ -F "file=@report.xlsx" "http://localhost:8000/api/v1/files/process?output_format=xlsx"
curl -F "file=@data.csv" "http://localhost:8000/api/v1/files/inspect?preview_rows=5"
```

Errors are returned as JSON `{"detail": "..."}`: `413` file too large, `415` unsupported file
type, `422` unreadable file or processing failure.

## Unit analysis

`POST /api/v1/unit/analyze` analyzes the test results of every unit (visual_id) in a raw export,
one row per test execution, and returns a `UnitAnalysis`
([app/schemas/unit.py](app/schemas/unit.py)) as JSON, following the methodology in
[review_material.md](review_material.md). The server owns all the business logic:
validation, classification, percentages, the Pareto and reconciliation are computed with
Polars, and the response holds facts only (no narrative). The LLM agent writes the summary and
recommendations from it. The same file always gives the same response.

```bash
# Every operation
curl -F "file=@units.csv" "http://localhost:8000/api/v1/unit/analyze"
# Some operations, top 20 rows in the HW-Induced Pareto
curl -F "file=@units.xlsx" "http://localhost:8000/api/v1/unit/analyze?operation=8748,7682&top_n=20"
# Drill-down: the pivot with each unit's classification, to open in Excel
curl -OJ -F "file=@units.csv" "http://localhost:8000/api/v1/unit/pivot?operation=8748&output_format=xlsx"
```

`operation` can be repeated or comma-separated. Without it, every operation is analyzed.

**Input**: the columns `visual_id`, `operation`, `within_session_sequence_number`, `data_bin` and
`device_end_date_time`, matched ignoring case, spaces and punctuation (`Visual ID`, `DATA-BIN`
work). The sequence column is also found as `sequence`, `sequence_number` or `seq`. Other
columns are ignored, and every value is read as text, so leading zeros are kept and `8748.0` reads as `8748`. `device_end_date_time` can
look like `10/13/2026 12:29:37 AM` (as Excel shows it), `10/13/2026 0:29:37`, `10/13/2026` or
`2026-10-13 00:29:37`, with any spacing; Excel date/time cells work as they are.

**Processing order**:

1. Rows failing a validation check are excluded.
2. Rows with `data_bin` = `5000000` are discarded. A unit left without any test isn't analyzed.
3. When a visual_id + sequence appears more than once in an operation, the row with the latest
   `device_end_date_time` is kept; on a tie, the first of the tied rows in the file. Because
   5000000 rows are discarded first, a later 5000000 retry never hides a real bin.
4. Each unit's remaining tests are pivoted by sequence and classified.

**Validation**. A row that fails a check is excluded from the analysis, and each failed check is
listed in `data_issues` as `<check>: <what was found>`:

| Check                                                             | Fails for                                                                                                                          |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Raw file successfully loaded                                      | A file without data rows. Empty rows are ignored                                                                                   |
| Required columns exist                                            | A missing column: nothing is analyzed                                                                                              |
| sequence is numeric                                               | A blank or non-numeric sequence                                                                                                    |
| Sequence numbers valid                                            | A sequence that isn't a whole number ≥ 1. Values above 4 are kept                                                                  |
| visual_id / operation / data_bin / device_end_date_time not blank | A blank value                                                                                                                      |
| device_end_date_time is a valid date and time                     | A date and time in none of the formats above                                                                                       |
| No corrupted rows                                                 | More fields than the header, control characters or undecodable bytes, or a data_bin that isn't a whole number (`#N/A`, `9.85E+07`) |
| No duplicate visual_id + sequence                                 | A repeat within an operation: the row with the latest `device_end_date_time` is kept                                               |
| Requested operations present                                      | A requested operation without rows to analyze (the others are analyzed)                                                            |

Problems with the data are reported in `data_issues` with a `200`, not as HTTP errors. The HTTP
errors are only for uploads that can't be read: `413`, `415` and `422` (e.g. a broken CSV quote).

**Classification** of each unit (visual_id) of an operation. The rules apply in this order and
the first match wins:

| Order | `rule`                     | Condition                                   | Classification    |
| ----- | -------------------------- | ------------------------------------------- | ----------------- |
| 1     | `rule1_recovery`           | A test has the pass bin `1000000`           | Recovery          |
| 2     | `rule2_sde_first_touch`    | Seq1 has an SDE bin (`809...`)              | True Reject       |
| 3     | `rule3_sde_later_sequence` | Seq1 isn't SDE, a later test has an SDE bin | HW-Induced Reject |
| 4     | `no_retest`                | Only one test                               | True Reject       |
| 5     | `rule4_repeatable_failure` | Seq1 = Seq2 (same fail bin)                 | True Reject       |
| 6     | `rule5_signature_changed`  | Seq1 != Seq2 (fail bin changed)             | HW-Induced Reject |

Seq1 and Seq2 are a unit's first two remaining tests in sequence order, so a discarded or missing
sequence doesn't break the comparison: Seq1 = 5000000, Seq2 = 98500000, Seq3 = 98500000 compares
98500000 with 98500000 (Rule 4). For the classification, later tests only matter for Rules 1 and 3.
The pivot still shows a discarded sequence as blank. The pass bin, discarded bin, SDE prefix,
typical sequence range and date/time formats are constants at the top of
[app/services/unit_analysis.py](app/services/unit_analysis.py).

**Output**: `filename`, `data_issues` and one entry per analyzed operation in `operations`, sorted
by volume (units):

```json
{
  "filename": "units.xlsx",
  "data_issues": ["device_end_date_time not blank: 1 row excluded"],
  "operations": [
    {
      "operation": "8748",
      "total_units": 25,
      "total_good_units": 4,
      "yield_pct": 16.0,
      "true_reject": 11,
      "true_reject_pct": 44.0,
      "hw_induced": 10,
      "hw_induced_pct": 40.0,
      "hw_induced_pareto": [
        {"from_bin": "98500000", "to_bin": "80045113", "count": 3, "pct": 25.0, "cumulative_pct": 25.0}
      ]
    }
  ]
}
```

| Field                            | Content                                                                                                                      |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `total_units`                    | Units (distinct visual_id) analyzed                                                                                          |
| `total_good_units`               | Units that passed (`1000000`) on any test: the Recovery units                                                                |
| `yield_pct`                      | Total Good Units, in % of Total Units                                                                                        |
| `true_reject`, `true_reject_pct` | True Reject units, and their % of Total Units                                                                                |
| `hw_induced`, `hw_induced_pct`   | HW-Induced Reject units, and their % of Total Units                                                                          |
| `hw_induced_pareto`              | Bin changes between consecutive tests (Seq1 → Seq2, Seq2 → Seq3, ...) of HW-Induced units, as data_bins, most frequent first |
| `data_issues`                    | Failed validation checks, or a failed final validation check. Empty when there is none                                       |

- Total Good Units + True Reject + HW-Induced = Total Units. The percentages are rounded to 2
  decimals, so Yield + True Reject % + HW-Induced % can be off 100% by 0.01.
- The Pareto keeps the top `top_n` rows (10 by default). A unit that changes bin twice counts
  twice; `pct` and `cumulative_pct` are shares of the operation's transitions.
- The methodology's final validation still runs on every operation (the counts add up to Total
  Units, no passing unit is counted as a reject, the SDE and Seq1/Seq2 rules were applied, the
  transitions and Pareto agree, the percentages are accurate). A failed check is added to
  `data_issues`, as `Operation 8748 final validation, <check>: <what was found>`.

For the agent: mention every `data_issues` entry, and use the metrics and the Pareto as given
rather than recomputing them.

### JSON endpoints for AI agents

`POST /api/v1/unit/analyze-json` and `POST /api/v1/unit/pivot-json` take the file base64-encoded
in a JSON body instead of a multipart/form-data upload. They are meant for AI agents and workflow
tools such as n8n, which build JSON requests reliably but not multipart file uploads.

```bash
printf '{"filename": "units.xlsx", "content_base64": "%s"}' "$(base64 -w0 units.xlsx)" |
  curl -H "Content-Type: application/json" --data-binary @- \
    "http://localhost:8000/api/v1/unit/analyze-json?operation=8748"
```

- The body has `filename`, whose extension sets the format (.csv, .xlsx or .xls), and
  `content_base64`, the file's bytes base64-encoded. Line breaks, missing padding, the URL-safe
  alphabet and a `data:...;base64,` prefix are accepted. Send `Content-Type: application/json`.
- Query parameters, checks, errors and response are shared with the multipart endpoints: the
  same file gives the same `UnitAnalysis` or pivot. Invalid base64 returns `422`.
- The file is decoded in memory and never written to disk. It can be up to `MAX_UPLOAD_SIZE_MB`
  once decoded; the JSON request itself can be up to 4/3 of that (base64) plus 1 MB.
- For very large files, prefer the multipart endpoints when the client supports them: base64
  makes the request a third larger, and the server holds it in memory during the analysis.
- In n8n, the Extract From File node's **Move File to Base64 String** operation turns a binary
  file into the string to send as `content_base64`. In an AI Agent tool, fill `content_base64`
  from the workflow with an expression and let the model choose only the query parameters.

## Adding your processing logic

Replace the example steps in `process_dataframe()` in [app/services/processing.py](app/services/processing.py):

```python
def process_dataframe(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.lazy()
        .filter(pl.col("amount") > 0)
        .group_by("customer")
        .agg(pl.col("amount").sum())
        .collect()
    )
```

- Keep the pipeline lazy (`df.lazy()` ... `.collect()`): `.collect()` runs on the engine selected by
  `POLARS_ENGINE`, and operations the GPU doesn't support fall back to CPU automatically.
- Polars errors raised here (e.g. a missing column) are returned to the client as `422` with the reason.
- New endpoints: add a router in `app/api/routes/` and register it in `app/api/main.py`; put request/response
  models in `app/schemas/` and logic in `app/services/`. Polars work is blocking, so run it with
  `anyio.to_thread.run_sync(..., limiter=limiter)` as `app/api/routes/files.py` does.

## Configuration

Settings are read from environment variables or `.env` (see [.env.example](.env.example)).

| Variable              | Default              | Description                                                                           |
| --------------------- | -------------------- | ------------------------------------------------------------------------------------- |
| `PROJECT_NAME`        | `Data Center Server` | Title shown in the API docs                                                           |
| `ENVIRONMENT`         | `local`              | `local`, `staging` or `production`                                                    |
| `LOG_LEVEL`           | `INFO`               | Log level of the `app.*` loggers                                                      |
| `API_V1_STR`          | `/api/v1`            | Prefix of all API routes                                                              |
| `CORS_ORIGINS`        | _(empty)_            | Comma-separated origins allowed from browsers; empty disables CORS                    |
| `MAX_UPLOAD_SIZE_MB`  | `200`                | Larger requests are rejected with `413`; JSON requests can be a third larger (base64) |
| `MAX_CONCURRENT_JOBS` | `2`                  | Files processed at the same time; extra requests wait their turn                      |
| `POLARS_ENGINE`       | `auto`               | `auto`, `in-memory`, `streaming` or `gpu`                                             |
| `APP_PORT`            | `8000`               | Docker Compose only: host port                                                        |
| `WEB_CONCURRENCY`     | `1`                  | Docker Compose only: Uvicorn worker processes                                         |

## Deployment with Docker

The host needs Docker with Compose v2.24+, plus the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
cp .env.example .env            # optional: override settings
docker compose up -d --build
docker compose ps               # STATUS shows "(healthy)" once the app is ready
curl http://localhost:8000/api/v1/health
docker compose logs -f api
```

- `docker-compose.yml` defaults to `ENVIRONMENT=production` and `POLARS_ENGINE=gpu`, and gives the
  container the host's GPUs. On a host without a GPU, remove the `deploy` and `volumes` blocks and set
  `POLARS_ENGINE=auto`.
- If the GPU can't be used at startup, the app logs a warning and runs on CPU; `/health` reports the
  engine actually in use. If it shows `auto` instead of `gpu`, the warning in `docker compose logs api`
  says why, and `docker compose exec api nvidia-smi` shows whether the container sees the GPU.
- Keep `WEB_CONCURRENCY=1` with the GPU engine (each worker process holds its own CUDA context) and
  tune `MAX_CONCURRENT_JOBS` to the RAM/VRAM available.
- The image is several GB because `polars[gpu]` ships the CUDA libraries.
- To deploy through a registry instead of building on the server, set `image:` to your registry path,
  run `docker compose build && docker compose push`, then `docker compose pull && docker compose up -d`
  on the server.
- Behind a reverse proxy (nginx, Traefik, ...) raise its request size limit and timeouts to match,
  e.g. nginx `client_max_body_size 200m;`.
