# LegoFlow Curator

Automated pipeline that converts GitHub PRs into verified SWE-Bench tasks across
8 programming languages: Python, JavaScript, TypeScript, Go, C, C++, Java, Rust.

LegoFlow Curator builds on [abundant-ai/SWE-gen](https://github.com/abundant-ai/SWE-gen)
and extends it into a curation pipeline: every task is not just verified, but also
**scored**, **tagged**, and produced concurrency-safely at farm scale.

## What LegoFlow Curator adds over SWE-gen

- **Static difficulty scoring** (`scoring.py`) — a 5-dimension, weighted,
  log-scaled model (patch scope, logic complexity, context breadth, test
  complexity, instruction complexity) that scores each task 1–10 with **no API
  calls**. It is the single source of truth shared by `create` and the dataset
  tagger, where SWE-gen only records a coarse LLM `easy/medium/hard` label.
- **4-tag metadata** — `create` writes
  `metadata.tags = [language, area, topic, bug_class]` into each `task.toml` in
  the same LLM call that evaluates the PR (no extra request). A canonical,
  resumable tagger (`tools/tag_task_metadata.py`) applies the same scoring + tags
  to existing JSONL datasets.
- **Multi-language PR collector** (`tools/collect_prs_wo_image.py`) — discovers
  qualifying repos/PRs across all 8 languages, with filter thresholds that are
  env-configurable (`LEGOFLOW_CURATOR_PR_*`) and per-language overrides.
- **Concurrency-safe repo cache** — cross-process file locking (`file_lock.py`)
  lets many workers share one on-disk repo cache without corruption.
- **Task-quality gating** — incomplete-task detection (`task_completion.py`
  catches unfilled test commands / TODO stubs) plus `--skip-quality-check`,
  `--no-require-issue`, and `--generate-name` controls.
- **Credential isolation** (`llm_env.py`, `api_logging.py`) — normalizes
  OpenAI/Anthropic base URLs and isolates runtime credentials, so the same
  pipeline runs against Anthropic-format or OpenAI-only/proxy providers.

## Pipeline

1. **Collect PRs** — discover qualifying GitHub repositories and PRs.
2. **Generate SWE tasks** — convert each PR into a Docker-based task with
   bug.patch / fix.patch / tests.
3. **Validate** — NOP/Oracle double-validation via Harbor.
4. **Score** — 5-dimension static difficulty scoring (Easy/Medium/Hard).
5. **Tag** — write `metadata.tags = [language, area, topic, bug_class]` into
   each task's `task.toml`.

## CLI commands

The `legoflow-curator` CLI exposes four commands (run
`legoflow-curator <cmd> --help` for flags):

- `create` — convert a list of PR IDs into verified, scored, tagged tasks.
- `validate` — re-run NOP/Oracle validation on an existing Harbor task.
- `analyze` — run agent trials on a task and classify the outcomes.
- `farm` — continuous PR farming: stream through a repo's entire PR history.

## Quick start

```bash
pip install -e .

# Credentials (or set them via the outer block's config.yaml).
export GITHUB_TOKENS="ghp_your_token"
export OPENAI_API_KEY="sk-your-key"
export OPENAI_API_BASE_URL="https://your-api.com/v1"

# Collect PRs
python tools/collect_prs_wo_image.py \
  --languages python --output_dir ./artifacts/collected_prs

# Generate verified tasks. --timeout is the OVERALL per-case budget and must be
# >= --cc-timeout (the inner Claude-Code session), or every case is killed early.
# --no-require-issue keeps PRs that don't link an issue.
legoflow-curator create \
  --input-ids-file ./artifacts/collected_prs/python_pr_ids.txt \
  --output ./artifacts/swe_tasks/py-cc \
  --n-concurrent 8 \
  --timeout 3200 \
  --cc-timeout 2400 \
  --no-require-issue
```

For full operational guidance (provider modes, launchers, dryrun, dashboard),
read the outer block's `CLAUDE.md`.

## task.toml metadata tags

`metadata.tags = [language, area, topic, bug_class]`:

- `language` — primary programming language.
- `area` — one of `backend`, `frontend`, `fullstack`, `cli`, `library`, `framework`.
- `topic` — framework/library name or focused technical topic.
- `bug_class` — domain-independent defect-mechanism label, e.g.
  `missing-fallback`, `incomplete-validation`, `wrong-default`.

The prompt and Pydantic schema live in `src/legoflow_curator/create/task_instruction.py`
and `src/legoflow_curator/create/utils.py`. Tags follow the taxonomy in
[`harbor/scripts/task_analysis`](https://github.com/SWE-Lego/harbor/tree/main/scripts/task_analysis).

## Acknowledgements

LegoFlow Curator is built on [abundant-ai/SWE-gen](https://github.com/abundant-ai/SWE-gen),
an Apache-2.0 project for converting merged GitHub PRs into Harbor tasks. The core
PR-to-task generation and validation flow originates there; LegoFlow Curator adds
the scoring, tagging, multi-language collection, and concurrency features described
above. Copyright and authorship remain available in the Git history.

## License

Apache-2.0
