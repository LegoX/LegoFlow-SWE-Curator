# LegoFlow Curator

LegoFlow Curator turns merged GitHub PRs into verified SWE-Bench tasks. It covers
8 languages: Python, JavaScript, TypeScript, Go, C, C++, Java, and Rust.

It grew out of [abundant-ai/SWE-gen](https://github.com/abundant-ai/SWE-gen). SWE-gen
gives you the core "reverse a PR into a runnable, test-verified task" flow. LegoFlow
Curator keeps that and wraps it in the pieces you need to actually curate a dataset:
scoring, tagging, bulk collection, and safe concurrent farming.

## What LegoFlow Curator adds over SWE-gen

Four things, in short:

1. **Difficulty scoring and tagging.** Every task gets a static difficulty score
   from `scoring.py`, a 5-dimension weighted model (patch scope, logic complexity,
   context breadth, test complexity, instruction length) that runs with no API
   calls. The same pass writes `metadata.tags = [language, area, topic, bug_class]`
   during the LLM call that already evaluates the PR, so tagging costs nothing
   extra. SWE-gen, by comparison, only keeps a coarse `easy/medium/hard` label.
   The scorer is shared by `create` and by `tools/tag_task_metadata.py`, a
   resumable tagger for JSONL datasets, so scores stay consistent everywhere.

2. **Multi-language PR collection.** `tools/collect_prs_wo_image.py` finds
   qualifying repos and PRs across all 8 languages in one run. Filter thresholds
   read from `LEGOFLOW_CURATOR_PR_*` environment variables and can be overridden
   per language, so you tune collection from config instead of editing code.

3. **Safe concurrent farming.** A cross-process file lock (`file_lock.py`) lets
   many workers share one on-disk repo cache without stepping on each other, which
   is what makes high `--n-concurrent` farming reliable.

4. **Quality gating and provider flexibility.** Incomplete tasks are caught before
   they ship: `task_completion.py` flags unfilled test commands and TODO stubs,
   and flags like `--skip-quality-check`, `--no-require-issue`, and
   `--generate-name` let you decide what counts as good enough. Runtime credentials
   are isolated and OpenAI/Anthropic base URLs are normalized (`llm_env.py`,
   `api_logging.py`), so the same pipeline runs against Anthropic-format or
   OpenAI-only proxy providers.

## Pipeline

1. Collect PRs: discover qualifying GitHub repositories and PRs.
2. Generate SWE tasks: convert each PR into a Docker-based task with
   bug.patch, fix.patch, and tests.
3. Validate: NOP/Oracle double-validation via Harbor.
4. Score: 5-dimension static difficulty scoring (Easy/Medium/Hard).
5. Tag: write `metadata.tags = [language, area, topic, bug_class]` into each
   task's `task.toml`.

## CLI commands

The `legoflow-curator` CLI has four commands. Run `legoflow-curator <cmd> --help`
for flags.

- `create`: convert a list of PR IDs into verified, scored, tagged tasks.
- `validate`: re-run NOP/Oracle validation on an existing Harbor task.
- `analyze`: run agent trials on a task and classify the outcomes.
- `farm`: continuous PR farming that streams through a repo's entire PR history.

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

- `language`: primary programming language.
- `area`: one of `backend`, `frontend`, `fullstack`, `cli`, `library`, `framework`.
- `topic`: framework/library name or focused technical topic.
- `bug_class`: domain-independent defect-mechanism label, for example
  `missing-fallback`, `incomplete-validation`, or `wrong-default`.

The prompt and Pydantic schema live in `src/legoflow_curator/create/task_instruction.py`
and `src/legoflow_curator/create/utils.py`. Tags follow the taxonomy in
[`harbor/scripts/task_analysis`](https://github.com/SWE-Lego/harbor/tree/main/scripts/task_analysis).

## Acknowledgements

LegoFlow Curator is built on [abundant-ai/SWE-gen](https://github.com/abundant-ai/SWE-gen),
an Apache-2.0 project for converting merged GitHub PRs into Harbor tasks. The core
PR-to-task generation and validation flow comes from there. LegoFlow Curator adds
the scoring, tagging, multi-language collection, and concurrency work described
above. Copyright and authorship remain available in the Git history.

## License

Apache-2.0
