#!/usr/bin/env python3
'''
Collect GitHub repositories with qualifying PRs for SWE-bench style dataset creation.

================================================================================
FILTERING CRITERIA (v2.7)
================================================================================

The thresholds below are the *default* values. They are read at import time from
LEGOFLOW_CURATOR_PR_* environment variables (see the "Environment" section), so the
LegoFlow outer block can drive them from config.yaml without editing this
file. Per-language entries in LANGUAGE_OVERRIDES (further down) take precedence
over both the defaults and the LEGOFLOW_CURATOR_PR_* env vars for the languages they cover.

Repository filters (defaults):
- target language > 40% of the codebase (via /repos/{owner}/{repo}/languages)
- >= 5 merged PRs
- >= 30 stars
- last push within ~3 years; non-archived; non-fork
- excludes awesome-*, tutorials, examples, demo, dotfiles, etc.

PR filters (defaults):
- linked issue resolved (closed); PR merged to the main branch
- PR links exactly one issue
- PR modifies both test files and non-test code
- 1-25 files changed; <= 1500 lines (additions + deletions) changed
- excludes dependency-bump PRs (bump, upgrade, renovate, dependabot, ...)
- excludes non-functional PRs (docs:, chore:, style:, ...) and revert/merge commits

Emits Multi-SWE-format records: org, repo, number, state, title, body, base,
resolved_issues, fix_patch, test_patch, instance_id, plus test-related fields
(fixed_tests, p2p_tests, f2p_tests, s2p_tests, n2p_tests, run_result,
test_patch_result, fix_patch_result) and provenance fields (base_commit,
language, pr_url, issue_url, merged_at, files_changed, merge_commit,
problem_statement, PR_id, ISSUE_id, ...).

Note: base_commit is the commit of the target branch *just before merge*, not the
target-branch commit at PR-creation time — these can diverge for long-lived PRs.

Usage:

    # Collect a few languages into ./collected_prs
    python tools/collect_prs_wo_image.py \
        --languages python javascript \
        --repo_num 500 \
        --max_prs_per_repo 50 \
        --output_dir ./collected_prs

Environment:
    GITHUB_TOKENS / GITHUB_TOKEN: comma- or whitespace-separated GitHub tokens.
                   Tokens are ALSO read from a token file (default: <repo>/gh_token.txt,
                   override with COLLECT_GITHUB_TOKEN_FILE). Env-var tokens and
                   file tokens are combined. Tokens are validated at startup;
                   invalid ones are filtered out. Parallel workers = valid tokens.
    LEGOFLOW_CURATOR_PR_*:   Override the default filter thresholds above, e.g.
                   LEGOFLOW_CURATOR_PR_MIN_STARS, LEGOFLOW_CURATOR_PR_MIN_MERGED_PRS,
                   LEGOFLOW_CURATOR_PR_MAX_LINES_CHANGED. LANGUAGE_OVERRIDES still win where set.
    GITHUB_TOKEN_PROXIES: Optional comma-separated token/proxy mappings:
                   "token1=http://user:pass@ip1:port,token2=socks5://user:pass@ip2:port"
    GITHUB_REQUIRE_PROXY_ISOLATION: Defaults to 1. When multiple tokens are used,
                   every token must have a fixed proxy mapping to avoid sharing one IP.

Proxy config files:
    gh_token.txt may contain either one token per line or "token proxy_url" per line.
    github_token_proxies.txt / gh_token_proxies.txt may contain "token proxy_url" mappings.

Output:
    - repos.jsonl: All qualifying repos (one per line)
    - prs.jsonl: All qualifying PRs with full details (one per line)
    - progress/: Directory containing progress state for resumable processing
'''

import os
import sys
import json
import requests
import argparse
import time
import re
import fcntl
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, RLock
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set, Tuple
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import hashlib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECT_GITHUB_TOKEN_FILE = os.environ.get(
    "COLLECT_GITHUB_TOKEN_FILE",
    str(PROJECT_ROOT / "gh_token.txt"),
)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
    # Import tqdm.write for safe logging in progress bar environments
    tqdm_write = tqdm.write
except ImportError:
    TQDM_AVAILABLE = False
    tqdm_write = print  # Fallback to regular print
    print("Warning: tqdm not installed. Progress bars will not be shown. Install with: pip install tqdm")


def log(message: str, lang: Optional[str] = None):
    """Print a log message with timestamp.
    
    Args:
        message: Log message
        lang: Optional language identifier for multi-process environments
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if lang:
        print(f"[{timestamp}][{lang}] {message}")
    else:
        print(f"[{timestamp}] {message}")


def append_pr_id_if_missing(pr_ids_file: str, pr_id: str) -> bool:
    """Append a PR ID exactly once, protected by a cross-process file lock."""
    os.makedirs(os.path.dirname(pr_ids_file) or ".", exist_ok=True)

    with open(pr_ids_file, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            existing_ids = {line.strip() for line in f if line.strip()}
            if pr_id in existing_ids:
                return False

            f.write(f"{pr_id}\n")
            f.flush()
            os.fsync(f.fileno())
            return True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def dedupe_pr_id_file(pr_ids_file: str) -> int:
    """Deduplicate a PR ID file in place while blocking concurrent writers."""
    if not os.path.exists(pr_ids_file):
        return 0

    with open(pr_ids_file, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            lines = [line.strip() for line in f if line.strip()]
            deduped = list(dict.fromkeys(lines))
            removed = len(lines) - len(deduped)
            if removed <= 0:
                return 0

            f.seek(0)
            f.truncate()
            for line in deduped:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
            return removed
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def dedupe_pr_id_files(output_dir: str, languages: List[str]) -> None:
    """Deduplicate local *_pr_ids.txt files in place before collection."""
    for lang in languages:
        pr_ids_file = os.path.join(output_dir, f"{lang}_pr_ids.txt")
        try:
            removed = dedupe_pr_id_file(pr_ids_file)
        except Exception:
            continue
        if removed > 0:
            log(f"Deduplicated {pr_ids_file}: removed {removed} duplicate IDs")


def format_recovery_time(seconds: float) -> str:
    """Format recovery time in seconds to human-readable string.
    
    Args:
        seconds: Time in seconds until recovery
        
    Returns:
        Formatted string like "X sec", "X min", or "X h Y min"
    """
    if seconds < 60:
        return f"{int(seconds)} sec"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} min"
    else:
        hours = int(seconds / 3600)
        remaining_minutes = int((seconds % 3600) / 60)
        if remaining_minutes > 0:
            return f"{hours} h {remaining_minutes} min"
        else:
            return f"{hours} h"


def mask_token(_token: str) -> str:
    """Return a fully redacted token marker for logs."""
    return "<redacted>"


def normalize_proxy_url(proxy_url: str) -> Optional[str]:
    """Normalize a proxy URL from config; return None for explicit direct mode."""
    proxy_url = proxy_url.strip()
    if not proxy_url or proxy_url.lower() in {"none", "direct", "-"}:
        return None
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    return proxy_url


def build_proxy_dict(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    """Build a requests-compatible proxy dict for both http and https."""
    proxy_url = normalize_proxy_url(proxy_url or "")
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def mask_proxy_url(proxy_url: str) -> str:
    """Mask proxy credentials before printing."""
    try:
        parts = urlsplit(proxy_url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"***@{host}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return proxy_url


def make_requests_session(proxy_dict: Optional[Dict[str, str]] = None) -> requests.Session:
    """Create a session isolated from ambient proxy environment variables."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Connection": "close"})
    if proxy_dict:
        session.proxies.update(proxy_dict)
    return session


def parse_token_proxy_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse token lines that may optionally include a fixed proxy URL."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None

    if "=" in line and not line.startswith(("http://", "https://", "socks4://", "socks5://")):
        token, proxy_url = line.split("=", 1)
        return token.strip(), proxy_url.strip()

    parts = line.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()

    return line, None


def load_collection_tokens_from_file(
    token_file: str = COLLECT_GITHUB_TOKEN_FILE,
) -> Tuple[List[str], Dict[str, Dict[str, str]]]:
    """Load collector tokens from gh_token.txt."""
    tokens: List[str] = []
    token_proxy_map: Dict[str, Dict[str, str]] = {}

    if not os.path.exists(token_file):
        return tokens, token_proxy_map

    with open(token_file, 'r', encoding='utf-8') as f:
        for line in f:
            token, proxy_url = parse_token_proxy_line(line)
            if not token:
                continue
            tokens.append(token)
            proxy_dict = build_proxy_dict(proxy_url)
            if proxy_dict:
                token_proxy_map[token] = proxy_dict

    return list(dict.fromkeys(tokens)), token_proxy_map


def load_collection_tokens_from_env() -> List[str]:
    """Load collector tokens from GITHUB_TOKENS or GITHUB_TOKEN."""
    env_tokens: List[str] = []
    tokens_str = os.environ.get("GITHUB_TOKENS", "")
    if tokens_str:
        env_tokens.extend(t.strip() for t in tokens_str.split(",") if t.strip())
    single_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if single_token:
        env_tokens.append(single_token)
    return list(dict.fromkeys(env_tokens))


def select_collection_tokens(
    tokens: List[str],
    token_proxy_map: Dict[str, Dict[str, str]],
    limit: int,
) -> Tuple[List[str], Dict[str, Dict[str, str]]]:
    """Limit startup token validation while keeping proxy mappings in sync."""
    if limit <= 0 or len(tokens) <= limit:
        selected = list(tokens)
    else:
        selected = list(tokens[:limit])

    selected_set = set(selected)
    return selected, {
        token: proxy_dict
        for token, proxy_dict in token_proxy_map.items()
        if token in selected_set
    }


def load_token_proxy_mapping_from_env() -> Dict[str, Dict[str, str]]:
    """Load fixed token->proxy mappings from GITHUB_TOKEN_PROXIES."""
    raw_mappings = os.environ.get("GITHUB_TOKEN_PROXIES", "")
    token_proxy_map: Dict[str, Dict[str, str]] = {}
    if not raw_mappings:
        return token_proxy_map

    entries = []
    for chunk in raw_mappings.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            entries.append(chunk)

    for entry in entries:
        token, proxy_url = parse_token_proxy_line(entry)
        proxy_dict = build_proxy_dict(proxy_url)
        if token and proxy_dict:
            token_proxy_map[token] = proxy_dict

    return token_proxy_map


def load_token_proxy_mapping_from_files(proxy_files: List[str]) -> Dict[str, Dict[str, str]]:
    """Load fixed token->proxy mappings from mapping files."""
    token_proxy_map: Dict[str, Dict[str, str]] = {}

    for proxy_file in proxy_files:
        if not os.path.exists(proxy_file):
            continue

        loaded = 0
        with open(proxy_file, "r", encoding="utf-8") as f:
            for raw_line in f:
                token, proxy_url = parse_token_proxy_line(raw_line)
                proxy_dict = build_proxy_dict(proxy_url)
                if token and proxy_dict:
                    token_proxy_map[token] = proxy_dict
                    loaded += 1

        print(f"Loaded {loaded} token proxy mapping(s) from {proxy_file}")

    return token_proxy_map


def log_proxy_isolation_summary(tokens: List[str], token_proxy_map: Dict[str, Dict[str, str]]) -> None:
    """Print a short non-secret summary of token/proxy binding."""
    mapped = sum(1 for token in tokens if token in token_proxy_map)
    log(f"Proxy isolation: {mapped}/{len(tokens)} token(s) have fixed proxy mappings")
    for token in tokens:
        proxy_dict = token_proxy_map.get(token)
        if not proxy_dict:
            log(f"  - {mask_token(token)} -> DIRECT")
            continue
        proxy_url = proxy_dict.get("https") or proxy_dict.get("http") or ""
        log(f"  - {mask_token(token)} -> {mask_proxy_url(proxy_url)}")

LANGUAGES = ['c', 'cpp', 'go', 'java', 'javascript', 'typescript', 'python', 'rust']
# LANGUAGES = ['go', 'javascript', 'typescript', 'python']
GITHUB_API_URL = "https://api.github.com"

# ============================================================================
# API REQUEST DELAY CONFIGURATION
# ============================================================================
# All delay times are centralized here for easy adjustment to reduce rate limiting.
# Adjust these values based on your token quota and rate limit situation.

# Base delay between API requests (in seconds)
API_REQUEST_DELAY = 0.3  # Reduced from 0.8 (99 tokens provide ample quota)
GITHUB_API_TIMEOUT = float(os.environ.get("GITHUB_API_TIMEOUT", "30"))
GITHUB_DIFF_TIMEOUT = float(os.environ.get("GITHUB_DIFF_TIMEOUT", "60"))

# Delays for specific operations (in seconds)
PR_LIST_DELAY = 0.2      # Reduced from 0.5
PR_FILES_DELAY = 0.2     # Reduced from 0.5
ISSUE_DELAY = 0.2        # Reduced from 0.5
DIFF_DELAY = 0.2         # Reduced from 0.5
COMMIT_DELAY = 0.2       # Reduced from 0.5

# Repository search delays (in seconds)
REPO_SEARCH_DELAY = 0.3  # Reduced from 0.5
REPO_PROCESS_DELAY = 0.1 # Reduced from 0.3

# PR processing delays (in seconds)
PR_SKIP_DELAY = 0.02     # Reduced from 0.05
REPO_COMPLETE_DELAY = 0.3  # Reduced from 1.0

# Error and retry delays (in seconds)
RATE_LIMIT_RETRY_DELAY = 15.0  # Delay when rate limited (before retry)
ERROR_RETRY_DELAY = 15.0       # Delay on error before retry
DIFF_RATE_LIMIT_DELAY = 15.0   # Delay for diff requests when rate limited

# Task cancellation delay (in seconds)
TASK_CANCELLATION_DELAY = 0.5  # Delay for cancelled tasks to finish

# ============================================================================
# FILTERING CONFIGURATION (v2.5 - Relaxed thresholds for more qualifying PRs)
# ============================================================================

# Filtering criteria version - increment this when changing filtering criteria
# This ensures that PRs are re-checked when criteria change
FILTERING_CRITERIA_VERSION = "v2.7"


def _env_int(name: str, default: int) -> int:
    """Read an int threshold from the environment, falling back to `default`.

    These LEGOFLOW_CURATOR_PR_* overrides let the LegoFlow block drive the global
    filtering thresholds from config.yaml (see scripts/load_runtime_env.sh)
    without editing this file. Empty/invalid values fall back to the default.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"WARNING: {name}={raw!r} is not an int; using default {default}")
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float threshold from the environment, falling back to `default`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"WARNING: {name}={raw!r} is not a float; using default {default}")
        return default


# Repository filtering thresholds (defaults). Each can be overridden globally
# via the matching LEGOFLOW_CURATOR_PR_* environment variable; per-language values in
# LANGUAGE_OVERRIDES below still take precedence when present.
MIN_STARS = _env_int("LEGOFLOW_CURATOR_PR_MIN_STARS", 30)                        # Minimum stars (relaxed from 50)
MIN_MERGED_PRS = _env_int("LEGOFLOW_CURATOR_PR_MIN_MERGED_PRS", 5)               # Minimum merged PRs
MIN_LANGUAGE_PERCENTAGE = _env_float("LEGOFLOW_CURATOR_PR_MIN_LANGUAGE_PERCENTAGE", 0.4)  # Target language must be >40% of codebase
MAX_DAYS_SINCE_PUSH = _env_int("LEGOFLOW_CURATOR_PR_MAX_DAYS_SINCE_PUSH", 1095)  # 3 years (per-language overrides below)

# PR filtering thresholds (defaults)
MIN_ISSUE_BODY_LENGTH = _env_int("LEGOFLOW_CURATOR_PR_MIN_ISSUE_BODY_LENGTH", 10)  # Minimum issue description length
MIN_PR_BODY_LENGTH = _env_int("LEGOFLOW_CURATOR_PR_MIN_PR_BODY_LENGTH", -1)        # No minimum PR body length
MAX_FILES_CHANGED = _env_int("LEGOFLOW_CURATOR_PR_MAX_FILES_CHANGED", 25)          # Maximum files changed (relaxed from 20)
MIN_FILES_CHANGED = _env_int("LEGOFLOW_CURATOR_PR_MIN_FILES_CHANGED", 1)           # Minimum files changed
MAX_LINES_CHANGED = _env_int("LEGOFLOW_CURATOR_PR_MAX_LINES_CHANGED", 1500)        # Maximum lines added + deleted (relaxed from 1000)

# Per-language overrides for languages that need tuned thresholds.
LANGUAGE_OVERRIDES = {
    'c': {
        'MIN_STARS': 10,
        'MIN_MERGED_PRS': 3,
        'MIN_LANGUAGE_PERCENTAGE': 0.25,
        'MAX_FILES_CHANGED': 35,
        'MAX_LINES_CHANGED': 2000,
        'MAX_DAYS_SINCE_PUSH': 1825,  # 5 years — classic C projects update slowly
    },
    'cpp': {
        'MIN_STARS': 10,
        'MIN_MERGED_PRS': 3,
        'MIN_LANGUAGE_PERCENTAGE': 0.25,
        'MAX_FILES_CHANGED': 35,
        'MAX_LINES_CHANGED': 2000,
        'MAX_DAYS_SINCE_PUSH': 1825,
    },
    'rust': {
        'MIN_STARS': 10,
        'MIN_MERGED_PRS': 3,
        'MIN_LANGUAGE_PERCENTAGE': 0.35,
        'MAX_FILES_CHANGED': 30,
        'MAX_LINES_CHANGED': 1500,
    },
    'java': {
        'MIN_STARS': 20,
        'MIN_MERGED_PRS': 3,
        'MIN_LANGUAGE_PERCENTAGE': 0.35,
        'MAX_FILES_CHANGED': 35,
        'MAX_LINES_CHANGED': 1800,
        'MAX_DAYS_SINCE_PUSH': 1460,  # 4 years — enterprise Java has long cycles
    },
    'go': {
        'MIN_STARS': 30,
        'MIN_MERGED_PRS': 5,
        'MAX_FILES_CHANGED': 30,
        'MAX_LINES_CHANGED': 1500,
    },
    'python': {
        'MIN_STARS': 30,
        'MIN_MERGED_PRS': 5,
        'MAX_FILES_CHANGED': 25,
        'MAX_LINES_CHANGED': 1500,
    },
    'typescript': {
        'MIN_STARS': 30,
        'MIN_MERGED_PRS': 5,
        'MIN_LANGUAGE_PERCENTAGE': 0.4,
        'MAX_FILES_CHANGED': 30,
        'MAX_LINES_CHANGED': 1500,
    },
    'javascript': {
        'MIN_STARS': 30,
        'MIN_MERGED_PRS': 5,
        'MIN_LANGUAGE_PERCENTAGE': 0.4,
        'MAX_FILES_CHANGED': 30,
        'MAX_LINES_CHANGED': 1500,
    },
}


def get_lang_config(language: str, key: str):
    """Get a filtering config value, with per-language overrides."""
    overrides = LANGUAGE_OVERRIDES.get(language, {})
    if key in overrides:
        return overrides[key]
    return globals()[key]

# Dependency management files by language (expanded for better coverage)
DEPENDENCY_FILES = {
    'python': ['requirements.txt', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Pipfile', 'poetry.lock', 'tox.ini', 'environment.yml', 'conda.yaml', 'pdm.lock'],
    'javascript': ['package.json', 'yarn.lock', 'package-lock.json', 'pnpm-lock.yaml', 'bun.lockb'],
    'typescript': ['package.json', 'tsconfig.json', 'yarn.lock', 'package-lock.json', 'pnpm-lock.yaml', 'bun.lockb'],
    'java': ['pom.xml', 'build.gradle', 'build.gradle.kts', 'settings.gradle', 'gradlew', 'mvnw'],
    'go': ['go.mod', 'go.sum', 'vendor', 'Gopkg.toml', 'Gopkg.lock'],
    'rust': ['Cargo.toml', 'Cargo.lock'],
    'c': ['CMakeLists.txt', 'Makefile', 'configure', 'configure.ac', 'meson.build', 'conanfile.txt', 'conanfile.py', 'autogen.sh', 'GNUmakefile', 'makefile', 'SConstruct', 'wscript'],
    'cpp': ['CMakeLists.txt', 'Makefile', 'configure', 'configure.ac', 'meson.build', 'conanfile.txt', 'conanfile.py', 'vcpkg.json', 'autogen.sh', 'GNUmakefile', 'makefile', 'SConstruct', 'wscript', 'BUILD.bazel', 'WORKSPACE'],
}

# CI/CD configuration files (expanded)
CI_CONFIG_FILES = [
    '.github/workflows',
    '.github',           # Any GitHub config
    '.travis.yml',
    '.circleci',
    'Jenkinsfile',
    '.gitlab-ci.yml',
    'azure-pipelines.yml',
    '.drone.yml',
    'bitbucket-pipelines.yml',
    'appveyor.yml',
    '.appveyor.yml',
    'tox.ini',           # Python testing
    'Makefile',          # Often contains test targets
    'CMakeLists.txt',    # CMake projects often have test targets
    '.pre-commit-config.yaml',
    'codecov.yml',
    '.codecov.yml',
]

# Repository name patterns to exclude
EXCLUDED_REPO_PATTERNS = [
    r'^awesome-',           # Awesome lists
    r'-awesome$',
    r'^tutorial',           # Tutorials
    r'tutorial$',
    r'^example',            # Examples
    r'example$',
    r'^demo',               # Demos
    r'demo$',
    r'^sample',             # Samples
    r'sample$',
    r'^dotfiles$',          # Dotfiles
    r'^\.',                 # Hidden repos
    r'^learn-',             # Learning repos
    r'-learn$',
    r'^how-to-',            # How-to guides
    r'^cookbook',           # Cookbooks
    r'^cheatsheet',         # Cheatsheets
    r'^boilerplate',        # Boilerplates (often just templates)
    r'^starter-',           # Starter templates
    r'-starter$',
    r'^template-',          # Templates
    r'-template$',
]

# PR title patterns to exclude (only critical non-functional changes)
# Relaxed: removed chore, style, ci, build patterns - they may contain valid code changes
EXCLUDED_PR_TITLE_PATTERNS = [
    r'^bump\s',             # Dependency bumps
    r'^\[?bump\]?\s*:?\s*v?\d',  # Bump version patterns
    r'renovate',            # Renovate bot
    r'dependabot',          # Dependabot
    r'greenkeeper',         # Greenkeeper
    r'snyk',                # Snyk security updates
    r'^revert\s',           # Reverts
    r'^revert:',
    r'^\[skip ci\]',
    r'^\[ci skip\]',
    r'^v?\d+\.\d+\.\d+$',   # Version number only titles (exact match)
]

# Test file patterns for different languages (expanded for better coverage)
TEST_FILE_PATTERNS = {
    'python': [
        r'test_.*\.py$', r'.*_test\.py$', r'tests?/.*\.py$', r'.*tests?\.py$',
        r'conftest\.py$', r'pytest.*\.py$', r'.*_tests\.py$',
        r'testing/.*\.py$',
    ],
    'javascript': [
        r'.*\.test\.js$', r'.*\.spec\.js$', r'__tests__/.*\.js$', r'test/.*\.js$', r'tests?/.*\.js$',
        r'.*\.test\.jsx$', r'.*\.spec\.jsx$', r'.*\.test\.mjs$', r'.*\.spec\.mjs$',
        r'.*\.e2e\.js$', r'.*\.e2e\.mjs$',
        r'cypress/.*\.js$', r'e2e/.*\.js$',
    ],
    'typescript': [
        r'.*\.test\.ts$', r'.*\.spec\.ts$', r'__tests__/.*\.ts$', r'test/.*\.ts$', r'tests?/.*\.ts$',
        r'.*\.test\.tsx$', r'.*\.spec\.tsx$',
        r'.*\.e2e\.ts$', r'.*\.e2e-spec\.ts$',
        r'cypress/.*\.ts$', r'e2e/.*\.ts$',
    ],
    'java': [
        r'.*Test\.java$', r'.*Tests\.java$', r'.*TestCase\.java$', r'src/test/.*\.java$',
        r'.*IT\.java$', r'.*ITCase\.java$',
        r'.*Spec\.java$', r'.*TestSuite\.java$',
    ],
    'go': [
        r'.*_test\.go$',
        r'testdata/.*',
    ],
    'rust': [
        r'tests?/.*\.rs$', r'.*_test\.rs$', r'.*_tests\.rs$',
        r'benches/.*\.rs$',
    ],
    'c': [
        r'test.*\.c$', r'.*_test\.c$', r'tests?/.*\.c$',
        r't/.*\.c$', r'check.*\.c$', r'.*_check\.c$',
        r'.*_unittest\.c$', r'unit_test.*\.c$',
    ],
    'cpp': [
        r'test.*\.cpp$', r'.*_test\.cpp$', r'tests?/.*\.cpp$', r'test.*\.cc$', r'.*_test\.cc$',
        r'.*_tests\.cpp$', r'.*_tests\.cc$', r'.*Test\.cpp$', r'.*Test\.cc$',
        r'gtest.*\.cpp$', r'.*_gtest\.cpp$',
        r'.*_unittest\.cpp$', r'.*_unittest\.cc$',
        r'test.*\.cxx$', r'.*_test\.cxx$',
    ],
}


def validate_github_tokens(
    tokens: List[str],
    token_proxy_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[List[str], Dict[str, dict]]:
    """
    Validate GitHub tokens by making a test API call.
    Returns tuple of (valid_tokens, token_rate_limits).
    
    Args:
        tokens: List of GitHub tokens to validate
        
    Returns:
        Tuple of (list of valid tokens, dict mapping token to rate limit info)
        Rate limit info dict contains: 'remaining' (int), 'reset_time' (int, Unix timestamp)
    """
    valid_tokens = []
    token_rate_limits = {}
    current_time = time.time()
    
    token_proxy_map = token_proxy_map or {}

    for token in tokens:
        try:
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            session = make_requests_session(token_proxy_map.get(token))
            response = None
            try:
                response = session.get(f"{GITHUB_API_URL}/user", headers=headers, timeout=10)
            
                # Extract rate limit info from headers (available in all responses)
                remaining_str = response.headers.get('X-RateLimit-Remaining', '0')
                reset_time_str = response.headers.get('X-RateLimit-Reset', '0')

                try:
                    remaining = int(remaining_str) if remaining_str != '?' else 0
                    reset_time = int(reset_time_str) if reset_time_str != '0' else 0
                except (ValueError, TypeError):
                    remaining = 0
                    reset_time = 0

                if response.status_code == 200:
                    print(f"  ✓ Token valid: {mask_token(token)} (remaining: {remaining})")
                    valid_tokens.append(token)
                    # Store rate limit info for valid tokens
                    token_rate_limits[token] = {
                        'remaining': remaining,
                        'reset_time': reset_time
                    }
                elif response.status_code == 401:
                    print(f"  ✗ Token invalid (401 Unauthorized): {mask_token(token)}")
                elif response.status_code == 403:
                    # Could be rate limited but still valid
                    if 'rate limit' in response.text.lower():
                        valid_tokens.append(token)
                        # Store rate limit info for rate-limited tokens
                        token_rate_limits[token] = {
                            'remaining': remaining,
                            'reset_time': reset_time
                        }
                        # Calculate and print recovery time
                        if reset_time > 0:
                            recovery_seconds = max(0, reset_time - current_time)
                            recovery_str = format_recovery_time(recovery_seconds)
                            print(f"  ⚠ Token rate limited but valid: {mask_token(token)} (recovery time: {recovery_str})")
                        else:
                            print(f"  ⚠ Token rate limited but valid: {mask_token(token)}")
                    elif 'suspended' in response.text.lower():
                        print(f"  ✗ Token account suspended: {mask_token(token)}")
                        # Don't add suspended tokens to rate_limits dict
                        continue
                    else:
                        print(f"  ✗ Token forbidden (403): {mask_token(token)}")
                else:
                    print(f"  ✗ Token check failed ({response.status_code}): {mask_token(token)}")
            finally:
                if response is not None:
                    response.close()
                session.close()
        except Exception as e:
            print(f"  ✗ Token check error: {mask_token(token)} - {e}")
            # Set default rate limit info for failed tokens
            token_rate_limits[token] = {
                'remaining': 0,
                'reset_time': 0
            }
    
    return valid_tokens, token_rate_limits


def check_token_rate_limits(
    tokens: List[str],
    token_proxy_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, dict]:
    """
    Check rate limit status for all tokens using the /rate_limit API endpoint.
    
    Args:
        tokens: List of GitHub tokens to check
        
    Returns:
        Dict mapping token to rate limit info containing:
        - 'remaining': remaining requests
        - 'limit': total limit
        - 'reset': reset time (Unix timestamp)
        - 'core': core rate limit info
    """
    rate_limit_info = {}
    current_time = time.time()
    
    token_proxy_map = token_proxy_map or {}

    for token in tokens:
        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json"
            }
            session = make_requests_session(token_proxy_map.get(token))
            response = None
            try:
                response = session.get(f"{GITHUB_API_URL}/rate_limit", headers=headers, timeout=10)
            
                if response.status_code == 200:
                    data = response.json()
                    core_info = data.get('resources', {}).get('core', {})
                    rate_limit_info[token] = {
                        'remaining': core_info.get('remaining', 0),
                        'limit': core_info.get('limit', 5000),
                        'reset': core_info.get('reset', 0),
                        'core': core_info
                    }
                else:
                    # If rate_limit endpoint fails, try to get info from headers
                    remaining_str = response.headers.get('X-RateLimit-Remaining', '0')
                    reset_time_str = response.headers.get('X-RateLimit-Reset', '0')
                    try:
                        remaining = int(remaining_str) if remaining_str != '?' else 0
                        reset_time = int(reset_time_str) if reset_time_str != '0' else 0
                    except (ValueError, TypeError):
                        remaining = 0
                        reset_time = 0
                    
                    rate_limit_info[token] = {
                        'remaining': remaining,
                        'limit': 5000,
                        'reset': reset_time,
                        'core': {}
                    }
            finally:
                if response is not None:
                    response.close()
                session.close()
        except Exception as e:
            # On error, set default values
            rate_limit_info[token] = {
                'remaining': 0,
                'limit': 5000,
                'reset': 0,
                'core': {}
            }
    return rate_limit_info


def check_all_tokens_rate_limited(valid_tokens: List[str], token_rate_limits: Dict[str, dict]) -> bool:
    """
    Check if all valid tokens are rate limited (remaining < 100).
    
    Args:
        valid_tokens: List of valid tokens
        token_rate_limits: Dict mapping token to rate limit info
        
    Returns:
        True if all tokens are rate limited, False otherwise
    """
    if not valid_tokens:
        return True
    
    MIN_QUOTA_THRESHOLD = 100
    all_rate_limited = True
    
    for token in valid_tokens:
        if token in token_rate_limits:
            remaining = token_rate_limits[token].get('remaining', 0)
            if remaining >= MIN_QUOTA_THRESHOLD:
                all_rate_limited = False
                break
    
    return all_rate_limited


@dataclass
class TokenManager:
    """Manages multiple GitHub tokens with rate limit tracking."""
    tokens: List[str] = field(default_factory=list)
    token_status: Dict[str, dict] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)
    current_index: int = 0
    initial_rate_limits: Optional[Dict[str, dict]] = field(default=None)
    max_concurrent_per_token: int = 1
    
    def __post_init__(self):
        for token in self.tokens:
            # Use initial rate limit info if provided, otherwise use defaults
            if self.initial_rate_limits and token in self.initial_rate_limits:
                rate_limit_info = self.initial_rate_limits[token]
                self.token_status[token] = {
                    'remaining': rate_limit_info.get('remaining', 5000),
                    'reset_time': rate_limit_info.get('reset_time', 0),
                    'is_valid': True,
                    'in_flight': 0
                }
            else:
                self.token_status[token] = {
                    'remaining': 5000,
                    'reset_time': 0,
                    'is_valid': True,
                    'in_flight': 0
                }
    
    def get_available_token(self) -> Optional[str]:
        """Get an available token with remaining rate limit.
        Prioritizes tokens with the most remaining quota to avoid exhausting tokens too quickly.
        """
        with self.lock:
            current_time = time.time()
            MIN_QUOTA_THRESHOLD = 100  # Don't use token if remaining < 100
            
            # First pass: find token with remaining quota, prioritizing tokens with more quota
            available_tokens = []
            has_token_with_quota = False
            
            for token in self.tokens:
                status = self.token_status[token]
                
                if not status['is_valid']:
                    continue
                
                # Check if rate limit has reset
                if status['remaining'] <= 0 and current_time < status['reset_time']:
                    continue
                
                # Reset if time has passed (only if reset_time is valid, i.e., > 0)
                if status['reset_time'] > 0 and current_time >= status['reset_time']:
                    status['remaining'] = 5000
                    status['reset_time'] = 0  # Clear reset_time after reset
                
                # Only consider tokens with quota above threshold
                if status['remaining'] >= MIN_QUOTA_THRESHOLD:
                    has_token_with_quota = True
                    if status.get('in_flight', 0) < self.max_concurrent_per_token:
                        available_tokens.append((token, status['remaining']))
            
            # If we have available tokens, return the one with the most remaining quota
            if available_tokens:
                # Sort by remaining quota (descending) and return the best one
                available_tokens.sort(key=lambda x: x[1], reverse=True)
                best_token = available_tokens[0][0]
                self.token_status[best_token]['in_flight'] = (
                    self.token_status[best_token].get('in_flight', 0) + 1
                )
                # Update current_index to the selected token for round-robin tracking
                try:
                    self.current_index = self.tokens.index(best_token)
                except ValueError:
                    pass
                return best_token

            if has_token_with_quota:
                # Tokens have quota but are currently checked out by other workers.
                return None, 1.0
            
            # All tokens exhausted or below threshold, find minimum wait time
            valid_statuses = [
                status['reset_time'] for status in self.token_status.values()
                if status['is_valid'] and status['reset_time'] > 0
            ]
            if not valid_statuses:
                # All tokens are invalid or have no reset time, wait a default time
                wait_time = 60  # Wait 60 seconds if all tokens are invalid
                return None, wait_time
            
            # Find the minimum reset time (earliest recovery)
            min_reset = min(valid_statuses)
            wait_time = max(0, min_reset - current_time)
            # Add small buffer (2 seconds) to ensure token is ready
            if wait_time > 0:
                wait_time += 2
            else:
                # If reset time has passed, wait a short time to allow token to be ready
                wait_time = 2
            return None, wait_time

    def release_token(self, token: str):
        """Release a token checked out by get_available_token."""
        with self.lock:
            if token in self.token_status:
                in_flight = self.token_status[token].get('in_flight', 0)
                self.token_status[token]['in_flight'] = max(0, in_flight - 1)
    
    def update_rate_limit(self, token: str, remaining: int, reset_time: int):
        """Update rate limit info for a token."""
        with self.lock:
            if token in self.token_status:
                self.token_status[token]['remaining'] = remaining
                self.token_status[token]['reset_time'] = reset_time
    
    def mark_invalid(self, token: str):
        """Mark a token as invalid (e.g., revoked)."""
        with self.lock:
            if token in self.token_status:
                self.token_status[token]['is_valid'] = False
    
    def get_rate_limited_tokens_info(self) -> List[Tuple[str, float]]:
        """Get information about rate-limited tokens.
        
        Returns:
            List of tuples (token_mask, recovery_seconds) for rate-limited tokens.
            token_mask is a masked version of the token (first 8 and last 4 chars).
        """
        with self.lock:
            current_time = time.time()
            rate_limited = []
            
            for token in self.tokens:
                if token not in self.token_status:
                    continue
                
                status = self.token_status[token]
                if not status['is_valid']:
                    continue
                
                # Check if token is rate limited
                if status['remaining'] <= 0:
                    if status['reset_time'] > 0:
                        recovery_seconds = max(0, status['reset_time'] - current_time)
                        token_mask = mask_token(token)
                        rate_limited.append((token_mask, recovery_seconds))
                    else:
                        # Rate limited but no reset time available
                        token_mask = mask_token(token)
                        rate_limited.append((token_mask, -1))  # -1 indicates unknown recovery time
            
            return rate_limited


class ProgressTracker:
    """Tracks processing progress for resumable operations."""
    
    def __init__(self, progress_dir: str, force_recheck_all: bool = False):
        self.progress_dir = progress_dir
        self.lock = Lock()
        self.force_recheck_all = force_recheck_all
        os.makedirs(progress_dir, exist_ok=True)
        
        # Track processed PRs per repo
        self.processed_prs_file = os.path.join(progress_dir, 'processed_prs.json')
        self.criteria_version_file = os.path.join(progress_dir, 'criteria_version.txt')
        self.processed_prs: Dict[str, Set[int]] = {}
        self._load_progress()

        # Per-repo recheck tracking: which repos have been fully re-examined
        # under the current FILTERING_CRITERIA_VERSION.
        self.rechecked_repos_file = os.path.join(progress_dir, 'rechecked_repos.json')
        self.rechecked_repos: Set[str] = set()
        self._load_rechecked_repos()

        # Check if filtering criteria have changed
        self.criteria_changed = self._check_criteria_changed()
        if self.criteria_changed:
            log(f"Filtering criteria version changed. All PRs will be re-checked.")
    
    def _load_progress(self):
        """Load progress from disk."""
        if os.path.exists(self.processed_prs_file):
            try:
                with open(self.processed_prs_file, 'r') as f:
                    data = json.load(f)
                    # Convert lists back to sets
                    self.processed_prs = {k: set(v) for k, v in data.items()}
            except (json.JSONDecodeError, IOError, ValueError) as e:
                # JSON file is corrupted, backup and start fresh
                log(f"Warning: Progress file {self.processed_prs_file} is corrupted: {e}")
                log(f"Backing up corrupted file and starting with empty progress...")
                
                # Backup the corrupted file
                try:
                    import shutil
                    backup_file = f"{self.processed_prs_file}.corrupted"
                    shutil.copy2(self.processed_prs_file, backup_file)
                    log(f"Corrupted file backed up to: {backup_file}")
                except Exception as backup_error:
                    log(f"Warning: Failed to backup corrupted file: {backup_error}")
                
                # Start with empty progress
                self.processed_prs = {}
                # Save empty progress to create a valid file
                self._save_progress()
                log(f"Created new empty progress file. Previous progress has been lost.")
    
    def _check_criteria_changed(self) -> bool:
        """Check if filtering criteria version has changed."""
        if self.force_recheck_all:
            return True

        current_version = FILTERING_CRITERIA_VERSION
        if os.path.exists(self.criteria_version_file):
            try:
                with open(self.criteria_version_file, 'r') as f:
                    saved_version = f.read().strip()
                if saved_version != current_version:
                    # Update version file so next run won't re-check everything
                    self._save_criteria_version()
                    return True
            except Exception as e:
                log(f"Warning: Could not read criteria version file: {e}")
                self._save_criteria_version()
                return True
        else:
            # First run, save current version
            self._save_criteria_version()
            return False
    
    def _save_criteria_version(self):
        """Save current filtering criteria version."""
        try:
            with open(self.criteria_version_file, 'w') as f:
                f.write(FILTERING_CRITERIA_VERSION)
        except Exception as e:
            log(f"Warning: Could not save criteria version: {e}")

    def _load_rechecked_repos(self):
        """Load set of repos fully re-examined under current criteria version."""
        if not os.path.exists(self.rechecked_repos_file):
            return
        try:
            with open(self.rechecked_repos_file, 'r') as f:
                data = json.load(f)
            if data.get('version') == FILTERING_CRITERIA_VERSION:
                self.rechecked_repos = set(data.get('repos', []))
                log(f"Loaded {len(self.rechecked_repos)} repos already rechecked under {FILTERING_CRITERIA_VERSION}")
            else:
                log(f"Rechecked repos file is from version {data.get('version')}, current is {FILTERING_CRITERIA_VERSION}. Clearing.")
                self.rechecked_repos = set()
        except Exception:
            self.rechecked_repos = set()

    def is_repo_rechecked(self, repo_full_name: str) -> bool:
        """True if this repo was fully processed under the current criteria version."""
        return repo_full_name in self.rechecked_repos

    def mark_repo_rechecked(self, repo_full_name: str):
        with self.lock:
            self.rechecked_repos.add(repo_full_name)
            if len(self.rechecked_repos) % 200 == 0:
                self._save_rechecked_repos_unlocked()

    def _save_rechecked_repos_unlocked(self):
        data = {'version': FILTERING_CRITERIA_VERSION, 'repos': list(self.rechecked_repos)}
        tmp = f"{self.rechecked_repos_file}.{os.getpid()}.tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.rechecked_repos_file)
        except Exception as e:
            log(f"Warning: could not save rechecked repos: {e}")

    def save_rechecked_repos(self):
        with self.lock:
            self._save_rechecked_repos_unlocked()

    def _save_progress(self):
        """Save progress to disk atomically (write tmp → fsync → rename)."""
        with self.lock:
            data = {k: list(v) for k, v in self.processed_prs.items()}
            tmp_file = f"{self.processed_prs_file}.{os.getpid()}.tmp"
            with open(tmp_file, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.processed_prs_file)

    def is_pr_processed(self, repo_full_name: str, pr_number: int) -> bool:
        """Check if a PR has been processed under the current criteria version.

        Returns False (= needs re-check) when:
          - force_recheck_all is set, OR
          - the repo has NOT been rechecked under the current criteria version
        """
        if self.force_recheck_all:
            return False
        if not self.is_repo_rechecked(repo_full_name):
            return False
        with self.lock:
            return pr_number in self.processed_prs.get(repo_full_name, set())

    def mark_pr_processed(self, repo_full_name: str, pr_number: int):
        """Mark a PR as processed."""
        with self.lock:
            if repo_full_name not in self.processed_prs:
                self.processed_prs[repo_full_name] = set()
            self.processed_prs[repo_full_name].add(pr_number)
            self._dirty_count = getattr(self, '_dirty_count', 0) + 1
        if self._dirty_count >= 50:
            self._dirty_count = 0
            self._save_progress()
    
    def get_last_processed_pr(self, repo_full_name: str) -> Optional[int]:
        """Get the last processed PR number for a repo (for pagination optimization)."""
        with self.lock:
            prs = self.processed_prs.get(repo_full_name, set())
            return max(prs) if prs else None


class GitHubClient:
    """GitHub API client with multi-token and fixed proxy support."""
    
    def __init__(
        self,
        token_manager: TokenManager,
        token_proxy_map: Optional[Dict[str, Dict[str, str]]] = None,
    ):
        self.token_manager = token_manager
        self.token_proxy_map = token_proxy_map or {}
        self.sessions = {
            token: make_requests_session(self.token_proxy_map.get(token))
            for token in self.token_manager.tokens
        }

    def _session_for_token(self, token: str) -> requests.Session:
        """Return the session pinned to this token's proxy."""
        if token not in self.sessions:
            self.sessions[token] = make_requests_session(self.token_proxy_map.get(token))
        return self.sessions[token]

    @staticmethod
    def _rate_limit_resource(url: str, response: requests.Response) -> str:
        """Return the GitHub rate-limit resource for a response.

        GitHub Search API uses a separate 30 req/min bucket. A successful search
        response with remaining=29 must not replace the token's core quota.
        """
        resource = response.headers.get("X-RateLimit-Resource", "").lower()
        if resource:
            return resource
        if "/search/" in url:
            return "search"
        return "core"

    def _update_token_rate_limit(
        self,
        token: str,
        url: str,
        response: requests.Response,
        *,
        force: bool = False,
    ) -> None:
        resource = self._rate_limit_resource(url, response)
        if resource == "search" and not force:
            return

        remaining = int(response.headers.get("X-RateLimit-Remaining", 5000))
        reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
        self.token_manager.update_rate_limit(token, remaining, reset_time)
    
    def make_request(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        """Make a GitHub API request with automatic token rotation."""
        max_retries = 3
        attempt = 0
        
        while attempt < max_retries:
            result = self.token_manager.get_available_token()
            
            if isinstance(result, tuple):
                # All tokens exhausted, need to wait
                _, wait_time = result
                # Get rate-limited tokens info and print recovery times
                rate_limited_info = self.token_manager.get_rate_limited_tokens_info()
                if rate_limited_info:
                    log(f"All tokens exhausted. Rate-limited tokens:")
                    for token_mask, recovery_seconds in rate_limited_info:
                        if recovery_seconds >= 0:
                            recovery_str = format_recovery_time(recovery_seconds)
                            log(f"  - {token_mask}: recovery time {recovery_str}")
                        else:
                            log(f"  - {token_mask}: recovery time unknown")
                elif wait_time > 1:
                    log(f"All tokens exhausted. Waiting {wait_time:.0f} seconds...")
                time.sleep(wait_time)
                continue
            
            token = result
            attempt += 1
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            try:
                response = None
                try:
                    session = self._session_for_token(token)
                    response = session.get(url, headers=headers, params=params, timeout=GITHUB_API_TIMEOUT)

                    self._update_token_rate_limit(token, url, response)

                    if response.status_code == 200:
                        # Add small delay between requests to avoid exhausting quota too quickly
                        # This helps distribute requests across time and reduces rate limiting
                        time.sleep(API_REQUEST_DELAY)
                        return response.json()
                    elif response.status_code == 403:
                        if 'rate limit' in response.text.lower():
                            # Rate limit hit, rotate silently
                            self._update_token_rate_limit(token, url, response, force=True)
                            continue
                        elif 'suspended' in response.text.lower():
                            self.token_manager.mark_invalid(token)
                            continue
                        else:
                            log(f"Forbidden: {response.text[:100]}")
                            return None
                    elif response.status_code == 404:
                        return None
                    elif response.status_code == 401:
                        log(f"Token invalid, marking as unusable")
                        self.token_manager.mark_invalid(token)
                        continue
                    elif response.status_code in (500, 502, 503, 504):
                        # Server error, retry silently
                        time.sleep(ERROR_RETRY_DELAY)
                        continue
                    else:
                        # Don't print HTML error pages
                        error_text = response.text[:100] if not response.text.strip().startswith('<!') else f"HTTP {response.status_code}"
                        if response.status_code not in (404, 422):  # Skip common expected errors
                            log(f"API error {response.status_code}: {error_text}")
                        return None
                finally:
                    if response is not None:
                        response.close()
                    self.token_manager.release_token(token)
                    
            except requests.exceptions.Timeout:
                # Retry silently on timeout
                time.sleep(ERROR_RETRY_DELAY)
            except Exception as e:
                # Only log unexpected errors
                if 'ConnectionError' not in str(type(e).__name__):
                    log(f"Request error: {type(e).__name__}")
                time.sleep(ERROR_RETRY_DELAY)
        
        return None
    
    def get_diff(self, url: str) -> Optional[str]:
        """Get diff content from a URL."""
        max_retries = 3
        attempt = 0
        
        while attempt < max_retries:
            result = self.token_manager.get_available_token()
            
            if isinstance(result, tuple):
                # All tokens exhausted, need to wait
                _, wait_time = result
                # Get rate-limited tokens info and print recovery times
                rate_limited_info = self.token_manager.get_rate_limited_tokens_info()
                if rate_limited_info:
                    log(f"All tokens exhausted (diff request). Rate-limited tokens:")
                    for token_mask, recovery_seconds in rate_limited_info:
                        if recovery_seconds >= 0:
                            recovery_str = format_recovery_time(recovery_seconds)
                            log(f"  - {token_mask}: recovery time {recovery_str}")
                        else:
                            log(f"  - {token_mask}: recovery time unknown")
                elif wait_time > 1:
                    log(f"All tokens busy (diff request). Waiting {wait_time:.0f} seconds...")
                # Wait for rate limit reset
                time.sleep(wait_time)
                continue  # Retry after waiting
            
            token = result
            attempt += 1
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3.diff"
            }
            
            try:
                response = None
                try:
                    session = self._session_for_token(token)
                    response = session.get(url, headers=headers, timeout=GITHUB_DIFF_TIMEOUT)

                    remaining = int(response.headers.get('X-RateLimit-Remaining', 5000))
                    reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                    self.token_manager.update_rate_limit(token, remaining, reset_time)

                    if response.status_code == 200:
                        # Add small delay between requests to avoid exhausting quota too quickly
                        time.sleep(API_REQUEST_DELAY)
                        return response.text
                    elif response.status_code == 403:
                        if 'rate limit' in response.text.lower():
                            # Rate limit hit, wait and retry
                            self.token_manager.update_rate_limit(token, 0, reset_time)
                            time.sleep(DIFF_RATE_LIMIT_DELAY)
                            continue
                        elif 'suspended' in response.text.lower():
                            self.token_manager.mark_invalid(token)
                            continue
                    # Silently handle other errors for diff retrieval
                finally:
                    if response is not None:
                        response.close()
                    self.token_manager.release_token(token)
            except requests.exceptions.Timeout:
                # Retry silently on timeout
                time.sleep(ERROR_RETRY_DELAY)
            except Exception:
                # Silently handle other errors
                pass
        
        return None


def is_test_file(filename: str, language: str) -> bool:
    """Check if a file is a test file based on language-specific patterns."""
    patterns = TEST_FILE_PATTERNS.get(language, [])
    for pattern in patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return True
    return False


def split_diff_by_test(diff_content: str, language: str) -> Tuple[str, str]:
    """Split diff content into code changes and test changes."""
    if not diff_content:
        return "", ""
    
    code_diffs = []
    test_diffs = []
    
    # Split by file sections
    file_sections = re.split(r'(?=^diff --git)', diff_content, flags=re.MULTILINE)
    
    for section in file_sections:
        if not section.strip():
            continue
        
        # Extract filename from diff header
        match = re.search(r'diff --git a/(.+?) b/', section)
        if not match:
            continue
        
        filename = match.group(1)
        
        if is_test_file(filename, language):
            test_diffs.append(section)
        else:
            code_diffs.append(section)
    
    return ''.join(code_diffs), ''.join(test_diffs)


def check_pr_criteria(pr_data: dict, files_data: list, language: str) -> Tuple[bool, str]:
    """
    Check if a PR meets all the criteria (OPTIMIZED v2.0).
    
    Criteria:
    - PR must be merged
    - File count in range [1, 10]
    - Total lines changed < 500
    - Must have both test files and code files modified
    - PR title must not match excluded patterns (dependency updates, chore, etc.)
    - PR body must be > 20 characters
    
    Returns:
        (is_valid, reason): Boolean and reason string if invalid
    """
    # PR must be merged
    if not pr_data.get('merged_at'):
        return False, "PR not merged"
    
    # Check PR title against excluded patterns
    pr_title = pr_data.get('title', '').lower()
    for pattern in EXCLUDED_PR_TITLE_PATTERNS:
        if re.search(pattern, pr_title, re.IGNORECASE):
            return False, f"PR title matches excluded pattern: {pattern}"
    
    # Check PR body length (must have meaningful description)
    # MIN_PR_BODY_LENGTH = -1 means no minimum requirement
    pr_body = pr_data.get('body', '') or ''
    if MIN_PR_BODY_LENGTH >= 0 and len(pr_body.strip()) < MIN_PR_BODY_LENGTH:
        return False, f"PR body too short ({len(pr_body)} < {MIN_PR_BODY_LENGTH} chars)"
    
    # Check number of files changed (1-20, relaxed from 1-15)
    num_files = len(files_data) if files_data else 0
    max_files = get_lang_config(language, 'MAX_FILES_CHANGED')
    if num_files < MIN_FILES_CHANGED or num_files > max_files:
        return False, f"File count {num_files} not in range [{MIN_FILES_CHANGED}, {max_files}]"

    # Check total lines changed (additions + deletions)
    total_additions = sum(f.get('additions', 0) for f in files_data)
    total_deletions = sum(f.get('deletions', 0) for f in files_data)
    total_changes = total_additions + total_deletions
    max_lines = get_lang_config(language, 'MAX_LINES_CHANGED')
    if total_changes > max_lines:
        return False, f"Too many lines changed ({total_changes} > {max_lines})"
    
    # Check for test files and code files
    has_test_files = False
    has_code_files = False
    
    for file_info in files_data:
        filename = file_info.get('filename', '')
        if is_test_file(filename, language):
            has_test_files = True
        else:
            has_code_files = True
    
    if not has_test_files:
        return False, "No test files modified"
    
    if not has_code_files:
        return False, "Only test files modified, no code changes"
    
    return True, ""


def get_linked_issue(client: GitHubClient, owner: str, repo: str, pr_number: int, pr_body: str) -> Optional[dict]:
    """
    Get the linked issue for a PR (OPTIMIZED v2.0).
    
    Checks:
    1. Issue linked via "fixes #123" or "closes #123" pattern in PR body
    2. Issue must be closed/resolved
    3. Issue description must be > 50 characters (was 10)
    4. PR should not be linked to multiple issues
    """
    if not pr_body:
        return None
    
    # Find issue references in PR body
    # Patterns: fixes #123, closes #123, resolves #123, fix #123, close #123, resolve #123
    pattern = r'(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)'
    matches = re.findall(pattern, pr_body.lower())
    
    if not matches:
        return None
    
    # Check for multiple issues (we want exactly one)
    unique_issues = list(set(matches))
    if len(unique_issues) > 1:
        return None  # Multiple issues linked
    
    issue_number = int(unique_issues[0])
    
    # Fetch issue details
    issue_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}"
    issue_data = client.make_request(issue_url)
    
    if not issue_data:
        return None
    
    # Check if issue is closed
    if issue_data.get('state') != 'closed':
        return None
    
    # Check issue body length (must be > 50 chars for meaningful description)
    issue_body = issue_data.get('body', '') or ''
    if len(issue_body.strip()) < MIN_ISSUE_BODY_LENGTH:
        return None
    
    return {
        'number': issue_number,
        'title': issue_data.get('title', ''),
        'body': issue_body,
        'state': issue_data.get('state'),
        'url': issue_data.get('html_url')
    }


def process_repo_prs(
    client: GitHubClient,
    repo_info: dict,
    progress: ProgressTracker,
    output_lock: Lock,
    prs_file: str,
    max_prs_per_repo: int = 50,
    repo_was_qualifying: bool = False
) -> Tuple[List[dict], dict]:
    """
    Process all PRs for a repository and return qualifying ones.
    
    Args:
        repo_was_qualifying: If True, this repo was already in qualifying_repos.json,
                           so we can skip already-processed PRs. If False, we need
                           to re-check all PRs (including previously processed ones)
                           because the filtering criteria may have changed.
    
    Returns:
        Tuple of (list of qualifying PRs, PR filtering statistics dict)
    """
    
    owner = repo_info['owner']
    repo_name = repo_info['repo']
    full_name = f"{owner}/{repo_name}"
    language = repo_info['language']
    
    qualifying_prs = []
    page = 1
    per_page = 100
    processed_count = 0
    
    # PR filtering statistics
    pr_stats = {
        'prs_scanned': 0,
        'prs_merged': 0,
        'prs_qualifying': 0,
        'filter_reasons': {
            'not_merged': 0,
            'excluded_title': 0,
            'short_pr_body': 0,
            'file_count_out_of_range': 0,
            'too_many_lines': 0,
            'no_test_files': 0,
            'only_test_files': 0,
            'no_linked_issue': 0,
            'issue_not_closed': 0,
            'short_issue_body': 0,
            'multiple_issues': 0,
            'no_diff': 0,
            'empty_patch': 0,
        }
    }
    
    while True:
        # Fetch PRs (merged only, sorted by updated date)
        prs_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pulls"
        params = {
            'state': 'closed',
            'sort': 'updated',
            'direction': 'desc',
            'per_page': per_page,
            'page': page
        }
        
        prs_data = client.make_request(prs_url, params)
        
        # Add delay after fetching PR list
        if prs_data:
            time.sleep(PR_LIST_DELAY)
        
        if not prs_data or len(prs_data) == 0:
            break
        
        for pr in prs_data:
            pr_number = pr['number']
            pr_stats['prs_scanned'] += 1
            
            # Skip PRs already checked under the current criteria version.
            # is_pr_processed returns False for repos not yet rechecked,
            # forcing a re-evaluation against the (possibly relaxed) criteria.
            if progress.is_pr_processed(full_name, pr_number):
                processed_count += 1
                continue
            
            # Quick check: must be merged
            if not pr.get('merged_at'):
                pr_stats['filter_reasons']['not_merged'] += 1
                progress.mark_pr_processed(full_name, pr_number)
                time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                continue
            
            pr_stats['prs_merged'] += 1
            
            # Get PR files
            files_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pulls/{pr_number}/files"
            files_data = client.make_request(files_url)
            
            # Add delay after fetching PR files
            if files_data:
                time.sleep(PR_FILES_DELAY)
            
            if not files_data:
                progress.mark_pr_processed(full_name, pr_number)
                time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                continue
            
            # Check basic PR criteria
            is_valid, reason = check_pr_criteria(pr, files_data, language)
            if not is_valid:
                # Map reason to filter category
                if 'title' in reason.lower():
                    pr_stats['filter_reasons']['excluded_title'] += 1
                elif 'pr body' in reason.lower():
                    pr_stats['filter_reasons']['short_pr_body'] += 1
                elif 'file count' in reason.lower():
                    pr_stats['filter_reasons']['file_count_out_of_range'] += 1
                elif 'lines' in reason.lower():
                    pr_stats['filter_reasons']['too_many_lines'] += 1
                elif 'no test' in reason.lower():
                    pr_stats['filter_reasons']['no_test_files'] += 1
                elif 'only test' in reason.lower():
                    pr_stats['filter_reasons']['only_test_files'] += 1
                progress.mark_pr_processed(full_name, pr_number)
                time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                continue
            
            # Check for linked issue
            pr_body = pr.get('body', '') or ''
            issue_info = get_linked_issue(client, owner, repo_name, pr_number, pr_body)
            
            # Add delay after fetching Issue
            if issue_info:
                time.sleep(ISSUE_DELAY)
            
            if not issue_info:
                # v2.4: Allow PRs without linked issues if PR body is descriptive enough
                pr_body_text = (pr.get('body', '') or '').strip()
                if len(pr_body_text) >= 50:
                    # Use PR body as problem statement instead of issue
                    issue_info = {
                        'number': None,
                        'title': pr.get('title', ''),
                        'body': pr_body_text,
                        'url': '',
                        'state': 'closed',
                        'from_pr_body': True,
                    }
                else:
                    pr_stats['filter_reasons']['no_linked_issue'] += 1
                    progress.mark_pr_processed(full_name, pr_number)
                    time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                    continue
            
            # Get full diff
            diff_url = pr.get('diff_url') or f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pulls/{pr_number}"
            diff_content = client.get_diff(diff_url)
            
            # Add delay after fetching diff
            if diff_content:
                time.sleep(DIFF_DELAY)
            
            if not diff_content:
                pr_stats['filter_reasons']['no_diff'] += 1
                progress.mark_pr_processed(full_name, pr_number)
                time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                continue
            
            # Split diff into code and test parts
            code_patch, test_patch = split_diff_by_test(diff_content, language)
            
            if not code_patch or not test_patch:
                pr_stats['filter_reasons']['empty_patch'] += 1
                progress.mark_pr_processed(full_name, pr_number)
                time.sleep(PR_SKIP_DELAY)  # Small delay when quickly skipping
                continue
            
            # Get base commit - the commit on target branch BEFORE the PR was merged
            # This is the "buggy" codebase that we want to use for training
            # 
            # Options:
            # 1. pr['base']['sha'] - target branch HEAD when PR was CREATED (❌ not what we want)
            # 2. pr['merge_commit_sha'] - the merge commit itself (❌ after merge)
            # 3. First parent of merge_commit_sha - target branch state before merge (✅ correct)
            #
            # We use merge_commit_sha's first parent to get the exact state before PR was merged
            merge_commit_sha = pr.get('merge_commit_sha', '')
            
            # Build base dict (Multi-SWE format)
            base_info = pr.get('base', {}).copy() if pr.get('base') else {}
            
            # Ensure base_info has the correct sha (base commit before merge)
            if merge_commit_sha:
                # Get the first parent of merge commit (target branch before merge)
                commit_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/commits/{merge_commit_sha}"
                commit_data = client.make_request(commit_url)
                if commit_data and commit_data.get('parents'):
                    # First parent is the target branch commit before merge
                    base_commit_sha = commit_data['parents'][0]['sha']
                    base_info['sha'] = base_commit_sha
                elif not base_info.get('sha'):
                    # Fallback: use base.sha from PR if available
                    base_info['sha'] = base_info.get('sha', '')
            elif not base_info.get('sha'):
                # Fallback for edge cases: ensure base.sha exists
                base_info['sha'] = base_info.get('sha', '')
            
            # Create PR record with Multi-SWE format fields + additional fields
            instance_id = f"{owner}__{repo_name}-{pr_number}"
            
            # Determine PR state: merged PRs should be "closed" in Multi-SWE format
            pr_state_raw = pr.get('state', '') or ('merged' if pr.get('merged_at') else 'closed')
            pr_state = 'closed' if pr.get('merged_at') else pr_state_raw
            
            # Build resolved_issues list (Multi-SWE format)
            resolved_issues = [{
                'number': issue_info['number'],
                'title': issue_info['title'],
                'body': issue_info['body']
            }]
            
            pr_record = {
                # Multi-SWE format fields
                'org': owner,
                'repo': repo_name,
                'number': pr_number,
                'state': pr_state,
                'title': pr.get('title', ''),
                'body': pr_body,
                'base': base_info,
                'resolved_issues': resolved_issues,
                'fix_patch': code_patch,
                'test_patch': test_patch,
                'instance_id': instance_id,
                # Test-related fields (empty, to be filled by test execution)
                'fixed_tests': {},
                'p2p_tests': {},
                'f2p_tests': {},
                's2p_tests': {},
                'n2p_tests': {},
                'run_result': {},
                'test_patch_result': {},
                'fix_patch_result': {},
                # Additional fields (preserved for compatibility, excluding duplicates)
                'language': language,
                'pr_url': pr.get('html_url', ''),
                'issue_url': issue_info['url'],
                'merged_at': pr.get('merged_at', ''),
                'files_changed': len(files_data),
                'merge_commit': merge_commit_sha,
                'patch': code_patch,  # Keep old field name for compatibility
            }
            
            # Write to file immediately (with lock)
            with output_lock:
                with open(prs_file, 'a') as f:
                    f.write(json.dumps(pr_record, ensure_ascii=False) + '\n')

                # Also write PR ID to the ID file (deduplicated)
                pr_id = f"{owner}/{repo_name}:pr-{pr_number}"
                # prs_file is {output_dir}/{lang}_prs.jsonl, so ID file is {output_dir}/{lang}_pr_ids.txt
                pr_ids_file = os.path.join(os.path.dirname(prs_file), f'{language}_pr_ids.txt')
                append_pr_id_if_missing(pr_ids_file, pr_id)
            
            qualifying_prs.append(pr_record)
            # Mark as processed (even if it was already processed before, this is idempotent)
            progress.mark_pr_processed(full_name, pr_number)
            pr_stats['prs_qualifying'] += 1
            
            # Limit PRs per repo to avoid spending too much time on one repo
            if len(qualifying_prs) >= max_prs_per_repo:
                break
        
        if len(qualifying_prs) >= max_prs_per_repo:
            break
        
        page += 1
        
        # Safety limit on pages
        if page > 20:
            break

    progress.mark_repo_rechecked(full_name)
    return qualifying_prs, pr_stats


def _process_single_repo(
    repo_info: dict,
    lang: str,
    client: GitHubClient,
    progress: 'ProgressTracker',
    output_lock: Lock,
    lang_prs_file: str,
    lang_repos_file: str,
    qualifying_tracker: 'QualifyingRepoTracker',
    filtering_stats: 'FilteringStatistics',
    repo_num: int,
    max_prs_per_repo: int,
    repo_pbar,
    qualifying_count_lock: Lock,
    qualifying_count_ref: list  # [qualifying_count, all_qualifying_prs, all_qualifying_repos]
) -> Tuple[bool, int]:
    """
    Process a single repo and return (has_qualifying_prs, num_prs_found).
    This function is designed to be called in parallel.
    """
    full_name = repo_info['full_name']
    
    try:
        # Check if we've reached the target (thread-safe check)
        with qualifying_count_lock:
            if qualifying_count_ref[0] >= repo_num:
                return False, 0
        
        # Update progress bar description with current repo
        if repo_pbar:
            with qualifying_count_lock:
                current_count = qualifying_count_ref[0]
            repo_pbar.set_postfix_str(f"{current_count}/{repo_num} qualifying")
            repo_display = full_name[:35] + "..." if len(full_name) > 35 else full_name
            repo_pbar.set_description(f"[{lang}] {repo_display}")
            sys.stdout.flush()
        
        # Check if this repo was already qualifying
        repo_was_qualifying = qualifying_tracker.is_repo_qualifying(lang, full_name)
        
        prs, pr_stats = process_repo_prs(
            client=client,
            repo_info=repo_info,
            progress=progress,
            output_lock=output_lock,
            prs_file=lang_prs_file,
            max_prs_per_repo=max_prs_per_repo,
            repo_was_qualifying=repo_was_qualifying
        )
        
        # Update PR statistics (thread-safe)
        filtering_stats.update_pr_stats(
            lang,
            prs_scanned=pr_stats['prs_scanned'],
            prs_merged=pr_stats['prs_merged'],
            prs_qualifying=pr_stats['prs_qualifying'],
            filter_reasons=pr_stats['filter_reasons']
        )
        
        if len(prs) > 0:
            # This repo has qualifying PRs!
            with qualifying_count_lock:
                # Double-check we haven't exceeded target
                if qualifying_count_ref[0] >= repo_num:
                    return False, len(prs)
                
                qualifying_tracker.add_qualifying_repo(lang, full_name)
                qualifying_count_ref[0] += 1
                qualifying_count_ref[1].extend(prs)  # all_qualifying_prs
                qualifying_count_ref[2].append(repo_info)  # all_qualifying_repos
                current_count = qualifying_count_ref[0]
            
            # Update repo with qualifying PRs count
            filtering_stats.update_repo_stats(lang, repos_with_qualifying_prs=1)
            
            # Save repo info to language-specific file
            with output_lock:
                with open(lang_repos_file, 'a') as f:
                    repo_info['qualifying_pr_count'] = len(prs)
                    f.write(json.dumps(repo_info, ensure_ascii=False) + '\n')
            
            # Update progress bar
            if repo_pbar:
                repo_pbar.set_postfix_str(f"{current_count}/{repo_num} qualifying")
                sys.stdout.flush()
            
            return True, len(prs)
        else:
            return False, 0
            
    except Exception as e:
        return False, 0


def verify_language_percentage(client: GitHubClient, owner: str, repo: str, language: str, min_percentage: float = 0.75) -> bool:
    """
    Verify that a language makes up at least min_percentage of the codebase.
    
    Args:
        client: GitHubClient instance
        owner: Repository owner
        repo: Repository name
        language: Target language to check
        min_percentage: Minimum percentage required (default 0.75 = 75%)
    
    Returns:
        True if the language is at least min_percentage of the codebase, False otherwise
    """
    lang_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/languages"
    lang_data = client.make_request(lang_url)
    
    if not lang_data:
        return False
    
    total_bytes = sum(lang_data.values())
    if total_bytes == 0:
        return False
    
    target_lang_bytes = lang_data.get(language, 0)
    percentage = target_lang_bytes / total_bytes
    
    return percentage >= min_percentage


def is_repo_name_excluded(repo_name: str) -> bool:
    """
    Check if a repository name matches excluded patterns.
    
    Excludes: awesome lists, tutorials, examples, demos, dotfiles, etc.
    """
    repo_lower = repo_name.lower()
    for pattern in EXCLUDED_REPO_PATTERNS:
        if re.search(pattern, repo_lower):
            return True
    return False


def check_repo_has_dependency_files(client: GitHubClient, owner: str, repo: str, language: str) -> bool:
    """
    Check if a repository has standard dependency management files for the language.
    
    RELAXED v2.2: For C/C++ languages, this check is optional (many C/C++ projects
    use Makefile/CMakeLists.txt as build system, which is sufficient).
    """
    dep_files = DEPENDENCY_FILES.get(language, [])
    if not dep_files:
        return True  # No specific files required for this language
    
    # RELAXED: For C/C++, allow repos without standard dependency files
    # Many C/C++ projects use Makefile/CMakeLists.txt as build system
    if language.lower() in ['c', 'cpp']:
        # Still check if they have build files (Makefile, CMakeLists.txt, etc.)
        # but don't require standard dependency management files
        contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents"
        contents = client.make_request(contents_url)
        
        if not contents:
            return True  # Allow if we can't check (relaxed)
        
        existing_files = {item.get('name', '').lower() for item in contents if item.get('type') == 'file'}
        # Check for common build files
        build_files = ['makefile', 'cmakelists.txt', 'configure', 'meson.build']
        if any(bf in existing_files for bf in build_files):
            return True
        # If no build files found, still allow (relaxed for C/C++)
        return True
    
    # Get repository contents (root level)
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents"
    contents = client.make_request(contents_url)
    
    if not contents:
        return False
    
    # Check if any dependency file exists
    existing_files = {item.get('name', '').lower() for item in contents if item.get('type') == 'file'}
    existing_dirs = {item.get('name', '').lower() for item in contents if item.get('type') == 'dir'}
    
    for dep_file in dep_files:
        dep_lower = dep_file.lower()
        if dep_lower in existing_files:
            return True
        # Some files might be in subdirectories (like .github/workflows)
        if '/' in dep_file:
            parent_dir = dep_file.split('/')[0].lower()
            if parent_dir in existing_dirs:
                return True
    
    return False


def check_repo_has_ci_config(client: GitHubClient, owner: str, repo: str) -> bool:
    """
    Check if a repository has CI/CD configuration files.
    """
    # Get repository contents (root level)
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents"
    contents = client.make_request(contents_url)
    
    if not contents:
        return False
    
    existing_files = {item.get('name', '').lower() for item in contents if item.get('type') == 'file'}
    existing_dirs = {item.get('name', '').lower() for item in contents if item.get('type') == 'dir'}
    
    for ci_file in CI_CONFIG_FILES:
        ci_lower = ci_file.lower()
        if ci_lower in existing_files or ci_lower in existing_dirs:
            return True
        # Check for .github directory (for GitHub Actions)
        if ci_lower.startswith('.github') and '.github' in existing_dirs:
            return True
    
    return False


def check_repo_recently_active(pushed_at: str, max_days: int = MAX_DAYS_SINCE_PUSH) -> bool:
    """
    Check if a repository was pushed to within the specified number of days.
    
    Args:
        pushed_at: ISO 8601 timestamp of last push
        max_days: Maximum days since last push (default: 1095 = 3 years)
    """
    if not pushed_at:
        return False
    
    try:
        # Parse ISO 8601 timestamp
        pushed_date = datetime.fromisoformat(pushed_at.replace('Z', '+00:00'))
        now = datetime.now(pushed_date.tzinfo)
        days_since_push = (now - pushed_date).days
        return days_since_push <= max_days
    except (ValueError, TypeError):
        return False


def get_candidate_repos(
    client: GitHubClient,
    language: str,
    min_stars: int = MIN_STARS,
    target_repo_count: int = 100,
    skip_full_names: Optional[Set[str]] = None,
    fast_pass_repos: Optional[Set[str]] = None,
    max_candidates_override: Optional[int] = None,
) -> Tuple[List[dict], dict]:
    """
    Get candidate repositories for a language that have merged PRs (OPTIMIZED v2.0).
    
    Uses dynamic search strategy based on target_repo_count to avoid excessive API requests.
    The search limit is calculated as max(target_repo_count * 5, 200) to ensure enough
    candidate repos are found (since not every candidate repo will have qualifying PRs).
    
    Criteria:
    - Language must be >60% of the codebase
    - Must have at least 20 merged PRs
    - Must have minimum 200 stars
    - Must have dependency management files (relaxed for C/C++)
    - Must be active within last 3 years
    - Not archived, not a fork
    - Repository name not in excluded patterns
    
    Args:
        client: GitHubClient instance
        language: Target language
        min_stars: Minimum stars required
        target_repo_count: Target number of qualifying repos needed (used to calculate search limit)
    
    Returns:
        Tuple of (list of candidate repos, filtering statistics dict)
    """
    # Calculate dynamic search limit based on target_repo_count
    # Multiply by 5 because not every candidate repo will have qualifying PRs
    # Minimum 200 to ensure reasonable search coverage
    max_candidates = max_candidates_override or max(target_repo_count * 5, 200)

    repos = []
    skip_full_names = skip_full_names or set()
    fast_pass_repos = fast_pass_repos or set()
    page = 1
    language_normalized = language.capitalize()  # Handle case variations (Python vs python)
    
    # Handle special cases for language names (must match GitHub API exactly)
    language_lower = language.lower()
    if language_lower == 'cpp':
        language_normalized = 'C++'
    elif language_lower == 'c':
        language_normalized = 'C'
    elif language_lower == 'javascript':
        language_normalized = 'JavaScript'  # GitHub API uses this exact casing
    elif language_lower == 'typescript':
        language_normalized = 'TypeScript'  # GitHub API uses this exact casing
    
    # Statistics tracking
    total_repos_seen = 0
    skipped_stats = {
        'archived': 0,
        'fork': 0,
        'excluded_name': 0,
        'low_stars': 0,
        'low_language_pct': 0,
        'low_pr_count': 0,
        'no_dep_files': 0,
        'no_ci_config': 0,
        'inactive': 0,
    }
    
    while len(repos) < max_candidates:
        # Segmented search by star ranges to bypass GitHub's 1000-result limit.
        # Fine-grained segments ensure we can discover repos beyond the 1000-result cap.
        star_segments = []
        if min_stars < 20:
            star_segments.append((min_stars, 19))
        if min_stars < 35:
            star_segments.append((max(min_stars, 20), 34))
        if min_stars < 50:
            star_segments.append((max(min_stars, 35), 49))
        if min_stars < 75:
            star_segments.append((max(min_stars, 50), 74))
        if min_stars < 100:
            star_segments.append((max(min_stars, 75), 99))
        if min_stars < 150:
            star_segments.append((max(min_stars, 100), 149))
        if min_stars < 200:
            star_segments.append((max(min_stars, 150), 199))
        if min_stars < 300:
            star_segments.append((max(min_stars, 200), 299))
        if min_stars < 500:
            star_segments.append((max(min_stars, 300), 499))
        star_segments.append((max(min_stars, 500), 749))
        star_segments.append((max(min_stars, 750), 999))
        star_segments.append((max(min_stars, 1000), 1999))
        star_segments.append((max(min_stars, 2000), 4999))
        star_segments.append((max(min_stars, 5000), 9999))
        star_segments.append((max(min_stars, 10000), 49999))
        star_segments.append((max(min_stars, 50000), 1000000))
        # Remove segments where low >= high
        star_segments = [(lo, hi) for lo, hi in star_segments if lo <= hi]

        for seg_lo, seg_hi in star_segments:
            if len(repos) >= max_candidates:
                break
            seg_page = 1
            query = f"language:{language} stars:{seg_lo}..{seg_hi} archived:false fork:false"
            while len(repos) < max_candidates:
                search_url = f"{GITHUB_API_URL}/search/repositories"
                params = {
                    'q': query,
                    'sort': 'stars',
                    'order': 'desc',
                    'per_page': 100,
                    'page': seg_page
                }
        
                data = client.make_request(search_url, params)

                # Add delay after each page of repository search
                if data:
                    time.sleep(REPO_SEARCH_DELAY)

                if not data or 'items' not in data:
                    break

                items = data['items']
                if not items:
                    break

                for item in items:
                    total_repos_seen += 1

                    if len(repos) >= max_candidates:
                        break

                    owner = item['owner']['login']
                    repo_name = item['name']
                    full_name = f"{owner}/{repo_name}"

                    if full_name in skip_full_names:
                        continue

                    # Fast-pass: repo was already processed under older criteria.
                    # Since criteria only got more relaxed, it still passes all
                    # repo-level filters. Skip the expensive API checks.
                    if full_name in fast_pass_repos:
                        pushed_at = item.get('pushed_at', '')
                        repos.append({
                            'owner': owner,
                            'repo': repo_name,
                            'full_name': full_name,
                            'stars': item['stargazers_count'],
                            'language': language,
                            'default_branch': item['default_branch'],
                            'pr_count': 0,
                            'pushed_at': pushed_at,
                        })
                        continue

                    if total_repos_seen % 100 == 0:
                        log(
                            f"[{language}] Candidate search progress: "
                            f"seen={total_repos_seen}, candidates={len(repos)}/{max_candidates}"
                        )

                    # Skip archived repos (double check)
                    if item.get('archived', False):
                        skipped_stats['archived'] += 1
                        continue

                    # Skip forks (double check)
                    if item.get('fork', False):
                        skipped_stats['fork'] += 1
                        continue

                    # Check repo name against excluded patterns
                    if is_repo_name_excluded(repo_name):
                        skipped_stats['excluded_name'] += 1
                        continue

                    # Check if recently active (per-language max days)
                    pushed_at = item.get('pushed_at', '')
                    lang_max_days = get_lang_config(language, 'MAX_DAYS_SINCE_PUSH')
                    if not check_repo_recently_active(pushed_at, max_days=lang_max_days):
                        skipped_stats['inactive'] += 1
                        continue

                    # Verify language percentage
                    lang_min_pct = get_lang_config(language, 'MIN_LANGUAGE_PERCENTAGE')
                    if not verify_language_percentage(client, owner, repo_name, language_normalized, min_percentage=lang_min_pct):
                        skipped_stats['low_language_pct'] += 1
                        continue

                    # Check for dependency management files
                    if not check_repo_has_dependency_files(client, owner, repo_name, language):
                        skipped_stats['no_dep_files'] += 1
                        continue

                    # Check for CI/CD configuration (optional — log but don't skip)
                    has_ci_config = check_repo_has_ci_config(client, owner, repo_name)
                    if not has_ci_config:
                        skipped_stats['no_ci_config'] += 1

                    # Check merged PR count using REST API (avoids Search API 30/min limit)
                    pr_list_url = f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pulls"
                    pr_params = {
                        'state': 'closed',
                        'per_page': 1,
                    }
                    pr_data = client.make_request(pr_list_url, pr_params)
                    lang_min_prs = get_lang_config(language, 'MIN_MERGED_PRS')
                    if not pr_data or not isinstance(pr_data, list) or len(pr_data) == 0:
                        skipped_stats['low_pr_count'] += 1
                        continue

                    repos.append({
                        'owner': owner,
                        'repo': repo_name,
                        'full_name': full_name,
                        'stars': item['stargazers_count'],
                        'language': language,
                        'default_branch': item['default_branch'],
                        'pr_count': 0,
                        'pushed_at': pushed_at,
                    })

                    time.sleep(REPO_PROCESS_DELAY)

                seg_page += 1
                if seg_page > 10:  # GitHub Search API returns max ~1000 results per query
                    break
        # Exit outer while loop after processing all segments
        break
    
    # Log concise summary
    log(f"[{language}] Found {len(repos)} candidate repos (searched {total_repos_seen})")
    
    # Return repos and statistics
    repo_stats = {
        'repos_searched': total_repos_seen,
        'repos_candidate': len(repos),
        'filter_reasons': skipped_stats,
    }
    
    return repos, repo_stats


def get_candidate_search_target(existing_count: int, repo_num: int) -> int:
    """Return how many additional qualifying repos this run still needs."""
    return max(0, repo_num - existing_count)


class QualifyingRepoTracker:
    """Tracks repos that have at least one qualifying PR."""
    
    def __init__(self, progress_dir: str):
        self.lock = Lock()
        self.qualifying_repos_file = os.path.join(progress_dir, 'qualifying_repos.json')
        self.qualifying_repos: Dict[str, Set[str]] = {}  # language -> set of repo full_names
        self._load()
    
    def _load(self):
        if os.path.exists(self.qualifying_repos_file):
            with open(self.qualifying_repos_file, 'r') as f:
                data = json.load(f)
                self.qualifying_repos = {k: set(v) for k, v in data.items()}
    
    def _save(self):
        with self.lock:
            data = {k: list(v) for k, v in self.qualifying_repos.items()}
            with open(self.qualifying_repos_file, 'w') as f:
                json.dump(data, f)
    
    def add_qualifying_repo(self, language: str, full_name: str):
        with self.lock:
            if language not in self.qualifying_repos:
                self.qualifying_repos[language] = set()
            self.qualifying_repos[language].add(full_name)
        self._save()
    
    def get_qualifying_count(self, language: str) -> int:
        with self.lock:
            return len(self.qualifying_repos.get(language, set()))

    def get_qualifying_repos(self, language: str) -> Set[str]:
        with self.lock:
            return set(self.qualifying_repos.get(language, set()))
    
    def is_repo_qualifying(self, language: str, full_name: str) -> bool:
        with self.lock:
            return full_name in self.qualifying_repos.get(language, set())


class FilteringStatistics:
    """
    Tracks filtering statistics for repos and PRs before/after filtering.
    This helps understand the pass rate for each language.
    """
    
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.lock = Lock()
        self.stats_file = os.path.join(output_dir, 'filtering_statistics.json')
        
        # Per-language statistics
        self.stats: Dict[str, dict] = {}
        self._load()
    
    def _load(self):
        """Load existing statistics from disk."""
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r') as f:
                    self.stats = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.stats = {}
    
    def _save(self):
        """Save statistics to disk."""
        with self.lock:
            with open(self.stats_file, 'w') as f:
                json.dump(self.stats, f, indent=2, ensure_ascii=False)
    
    def init_language(self, language: str):
        """Initialize statistics for a language if not exists."""
        with self.lock:
            if language not in self.stats:
                self.stats[language] = {
                    # Repository statistics
                    'repos_searched': 0,           # Total repos searched from GitHub API
                    'repos_candidate': 0,          # Repos passing initial filters (candidates)
                    'repos_with_qualifying_prs': 0, # Repos with at least 1 qualifying PR
                    'repo_filter_reasons': {       # Why repos were filtered out
                        'archived': 0,
                        'fork': 0,
                        'excluded_name': 0,
                        'inactive': 0,
                        'low_language_pct': 0,
                        'no_dep_files': 0,
                        'no_ci_config': 0,
                        'low_pr_count': 0,
                    },
                    # PR statistics
                    'prs_scanned': 0,              # Total PRs scanned
                    'prs_merged': 0,               # PRs that were merged
                    'prs_qualifying': 0,           # PRs passing all filters
                    'pr_filter_reasons': {         # Why PRs were filtered out
                        'not_merged': 0,
                        'excluded_title': 0,
                        'short_pr_body': 0,
                        'file_count_out_of_range': 0,
                        'too_many_lines': 0,
                        'no_test_files': 0,
                        'only_test_files': 0,
                        'no_linked_issue': 0,
                        'issue_not_closed': 0,
                        'short_issue_body': 0,
                        'multiple_issues': 0,
                        'no_diff': 0,
                        'empty_patch': 0,
                    },
                    # Timestamps
                    'last_updated': None,
                }
        self._save()
    
    def update_repo_stats(self, language: str, 
                          repos_searched: int = 0,
                          repos_candidate: int = 0,
                          repos_with_qualifying_prs: int = 0,
                          filter_reasons: Optional[dict] = None):
        """Update repository statistics for a language."""
        with self.lock:
            if language not in self.stats:
                self.init_language(language)
            
            self.stats[language]['repos_searched'] += repos_searched
            self.stats[language]['repos_candidate'] += repos_candidate
            self.stats[language]['repos_with_qualifying_prs'] += repos_with_qualifying_prs
            
            if filter_reasons:
                for reason, count in filter_reasons.items():
                    if reason in self.stats[language]['repo_filter_reasons']:
                        self.stats[language]['repo_filter_reasons'][reason] += count
            
            self.stats[language]['last_updated'] = datetime.now().isoformat()
        self._save()
    
    def update_pr_stats(self, language: str,
                        prs_scanned: int = 0,
                        prs_merged: int = 0,
                        prs_qualifying: int = 0,
                        filter_reasons: Optional[dict] = None):
        """Update PR statistics for a language."""
        with self.lock:
            if language not in self.stats:
                self.init_language(language)
            
            self.stats[language]['prs_scanned'] += prs_scanned
            self.stats[language]['prs_merged'] += prs_merged
            self.stats[language]['prs_qualifying'] += prs_qualifying
            
            if filter_reasons:
                for reason, count in filter_reasons.items():
                    if reason in self.stats[language]['pr_filter_reasons']:
                        self.stats[language]['pr_filter_reasons'][reason] += count
            
            self.stats[language]['last_updated'] = datetime.now().isoformat()
        self._save()
    
    def get_summary(self) -> dict:
        """Get a summary of all statistics with pass rates."""
        summary = {
            'generated_at': datetime.now().isoformat(),
            'languages': {},
            'totals': {
                'repos_searched': 0,
                'repos_candidate': 0,
                'repos_with_qualifying_prs': 0,
                'prs_scanned': 0,
                'prs_merged': 0,
                'prs_qualifying': 0,
            }
        }
        
        for lang, stats in self.stats.items():
            repos_searched = stats.get('repos_searched', 0)
            repos_candidate = stats.get('repos_candidate', 0)
            repos_qualifying = stats.get('repos_with_qualifying_prs', 0)
            prs_scanned = stats.get('prs_scanned', 0)
            prs_merged = stats.get('prs_merged', 0)
            prs_qualifying = stats.get('prs_qualifying', 0)
            
            # Calculate pass rates
            repo_candidate_rate = (repos_candidate / repos_searched * 100) if repos_searched > 0 else 0
            repo_qualifying_rate = (repos_qualifying / repos_candidate * 100) if repos_candidate > 0 else 0
            pr_merge_rate = (prs_merged / prs_scanned * 100) if prs_scanned > 0 else 0
            pr_qualifying_rate = (prs_qualifying / prs_merged * 100) if prs_merged > 0 else 0
            
            summary['languages'][lang] = {
                **stats,
                'pass_rates': {
                    'repo_candidate_rate': f"{repo_candidate_rate:.2f}%",
                    'repo_qualifying_rate': f"{repo_qualifying_rate:.2f}%",
                    'pr_merge_rate': f"{pr_merge_rate:.2f}%",
                    'pr_qualifying_rate': f"{pr_qualifying_rate:.2f}%",
                }
            }
            
            # Update totals
            summary['totals']['repos_searched'] += repos_searched
            summary['totals']['repos_candidate'] += repos_candidate
            summary['totals']['repos_with_qualifying_prs'] += repos_qualifying
            summary['totals']['prs_scanned'] += prs_scanned
            summary['totals']['prs_merged'] += prs_merged
            summary['totals']['prs_qualifying'] += prs_qualifying
        
        # Calculate total pass rates
        t = summary['totals']
        t['repo_candidate_rate'] = f"{(t['repos_candidate'] / t['repos_searched'] * 100) if t['repos_searched'] > 0 else 0:.2f}%"
        t['repo_qualifying_rate'] = f"{(t['repos_with_qualifying_prs'] / t['repos_candidate'] * 100) if t['repos_candidate'] > 0 else 0:.2f}%"
        t['pr_merge_rate'] = f"{(t['prs_merged'] / t['prs_scanned'] * 100) if t['prs_scanned'] > 0 else 0:.2f}%"
        t['pr_qualifying_rate'] = f"{(t['prs_qualifying'] / t['prs_merged'] * 100) if t['prs_merged'] > 0 else 0:.2f}%"
        
        return summary
    
    def save_summary_report(self):
        """Save a human-readable summary report."""
        summary = self.get_summary()
        report_file = os.path.join(self.output_dir, 'filtering_report.md')
        
        with open(report_file, 'w') as f:
            f.write("# SWE-Bench Data Collection - Filtering Statistics Report\n\n")
            f.write(f"Generated at: {summary['generated_at']}\n\n")
            
            f.write("## Overall Summary\n\n")
            t = summary['totals']
            f.write("| Metric | Count | Pass Rate |\n")
            f.write("|--------|-------|-----------|\n")
            f.write(f"| Repos Searched | {t['repos_searched']} | - |\n")
            f.write(f"| Repos Candidate | {t['repos_candidate']} | {t['repo_candidate_rate']} |\n")
            f.write(f"| Repos with Qualifying PRs | {t['repos_with_qualifying_prs']} | {t['repo_qualifying_rate']} |\n")
            f.write(f"| PRs Scanned | {t['prs_scanned']} | - |\n")
            f.write(f"| PRs Merged | {t['prs_merged']} | {t['pr_merge_rate']} |\n")
            f.write(f"| PRs Qualifying | {t['prs_qualifying']} | {t['pr_qualifying_rate']} |\n")
            f.write("\n")
            
            f.write("## Per-Language Statistics\n\n")
            for lang in sorted(summary['languages'].keys()):
                lang_stats = summary['languages'][lang]
                rates = lang_stats['pass_rates']
                
                f.write(f"### {lang.upper()}\n\n")
                f.write("**Repository Filtering:**\n\n")
                f.write(f"- Searched: {lang_stats.get('repos_searched', 0)}\n")
                f.write(f"- Candidates: {lang_stats.get('repos_candidate', 0)} ({rates['repo_candidate_rate']})\n")
                f.write(f"- With Qualifying PRs: {lang_stats.get('repos_with_qualifying_prs', 0)} ({rates['repo_qualifying_rate']})\n\n")
                
                # Repo filter reasons
                repo_reasons = lang_stats.get('repo_filter_reasons', {})
                if any(v > 0 for v in repo_reasons.values()):
                    f.write("Repos filtered out by reason:\n")
                    for reason, count in sorted(repo_reasons.items(), key=lambda x: -x[1]):
                        if count > 0:
                            f.write(f"  - {reason}: {count}\n")
                    f.write("\n")
                
                f.write("**PR Filtering:**\n\n")
                f.write(f"- Scanned: {lang_stats.get('prs_scanned', 0)}\n")
                f.write(f"- Merged: {lang_stats.get('prs_merged', 0)} ({rates['pr_merge_rate']})\n")
                f.write(f"- Qualifying: {lang_stats.get('prs_qualifying', 0)} ({rates['pr_qualifying_rate']})\n\n")
                
                # PR filter reasons
                pr_reasons = lang_stats.get('pr_filter_reasons', {})
                if any(v > 0 for v in pr_reasons.values()):
                    f.write("PRs filtered out by reason:\n")
                    for reason, count in sorted(pr_reasons.items(), key=lambda x: -x[1]):
                        if count > 0:
                            f.write(f"  - {reason}: {count}\n")
                    f.write("\n")
                
                f.write("---\n\n")
        
        log(f"Filtering report saved to: {report_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Collect GitHub repositories and their qualifying PRs for SWE-bench style datasets."
    )
    parser.add_argument(
        "--repo_num", type=int, default=10,
        help="Number of repositories WITH QUALIFYING PRs to collect per language (not candidate repos)."
    )
    parser.add_argument(
        "--output_dir", type=str, default="collected_prs",
        help="Directory to save collected data."
    )
    parser.add_argument(
        "--languages", type=str, default=None,
        help="Comma-separated list of languages to process (default: all). "
             "For multi-process usage, specify only ONE language per process."
    )
    parser.add_argument(
        "--disable_progress_bar", action="store_true",
        help="Disable progress bars (useful for multi-process environments)"
    )
    parser.add_argument(
        "--max_prs_per_repo", type=int, default=50,
        help="Maximum QUALIFYING PRs to collect per repository"
    )
    parser.add_argument(
        "--max-candidate-repos", type=int, default=None,
        help=(
            "Maximum candidate repositories to inspect per language. "
            "Defaults to a broad batch-oriented search; set a small value for smoke tests."
        ),
    )
    parser.add_argument(
        "--force-recheck-all", action="store_true",
        help="Force re-check all PRs, even if they were processed before. "
             "Useful when filtering criteria have changed."
    )
    
    args = parser.parse_args()
    
    tokens, token_proxy_map = load_collection_tokens_from_file(COLLECT_GITHUB_TOKEN_FILE)
    proxy_loaded = len(token_proxy_map)
    print(
        f"Loaded {len(tokens)} tokens from {COLLECT_GITHUB_TOKEN_FILE} "
        f"({proxy_loaded} with inline proxy)"
    )

    env_tokens = load_collection_tokens_from_env()
    if env_tokens:
        tokens.extend(env_tokens)
        print(f"Loaded {len(env_tokens)} tokens from GITHUB_TOKENS/GITHUB_TOKEN env")

    # From explicit proxy mapping sources. These override inline mappings.
    # Files are resolved relative to the repo root (override with
    # COLLECT_TOKEN_PROXY_FILES, a ':'-separated path list).
    token_proxy_map.update(load_token_proxy_mapping_from_env())
    _default_proxy_files = [
        str(PROJECT_ROOT / 'github_token_proxies.txt'),
        str(PROJECT_ROOT / 'gh_token_proxies.txt'),
        str(PROJECT_ROOT / 'token_proxies.txt'),
    ]
    _proxy_files_env = os.environ.get('COLLECT_TOKEN_PROXY_FILES', '')
    proxy_files = _proxy_files_env.split(':') if _proxy_files_env else _default_proxy_files
    token_proxy_map.update(load_token_proxy_mapping_from_files(proxy_files))

    # Deduplicate while preserving order
    tokens = list(dict.fromkeys(tokens))
    print(f"Total unique tokens: {len(tokens)}")

    if not tokens:
        print(f"Error: No GitHub tokens found. Provide {COLLECT_GITHUB_TOKEN_FILE}.")
        return

    token_limit_raw = os.environ.get("COLLECT_TOKEN_LIMIT", "32").strip()
    try:
        token_limit = int(token_limit_raw)
    except ValueError:
        token_limit = 32
    original_token_count = len(tokens)
    tokens, token_proxy_map = select_collection_tokens(tokens, token_proxy_map, token_limit)
    if token_limit > 0 and len(tokens) < original_token_count:
        print(
            f"Using first {len(tokens)}/{original_token_count} token(s) for this collector "
            f"(COLLECT_TOKEN_LIMIT={token_limit}; set 0 to validate all)."
        )

    require_proxy_isolation = os.environ.get(
        'GITHUB_REQUIRE_PROXY_ISOLATION', '1'
    ).strip().lower() not in {'0', 'false', 'no', 'off'}

    if require_proxy_isolation and len(tokens) > 1:
        missing_proxy_tokens = [token for token in tokens if token not in token_proxy_map]
        if missing_proxy_tokens:
            original_token_count = len(tokens)
            tokens = [tokens[0]]
            token_proxy_map = {
                token: proxy_dict
                for token, proxy_dict in token_proxy_map.items()
                if token in tokens
            }
            print("\nWARNING: multiple GitHub tokens were found, but fixed proxy mappings are missing.")
            print(
                f"To keep IP isolation safe, this run will use only 1/{original_token_count} token(s): "
                f"{mask_token(tokens[0])}"
            )
            print("To use all tokens, add proxies using one of these formats:")
            print("  gh_token.txt line:              <token> <proxy_url>")
            print("  github_token_proxies.txt line:  <token> <proxy_url>")
            print("  GITHUB_TOKEN_PROXIES env:       token1=http://proxy1,token2=socks5://proxy2")
            print("To intentionally use all tokens from one IP, set GITHUB_REQUIRE_PROXY_ISOLATION=0.")

    log_proxy_isolation_summary(tokens, token_proxy_map)
    
    print(f"Found {len(tokens)} GitHub token(s), validating...")
    
    # Validate tokens and filter out invalid ones, get rate limit info
    valid_tokens, token_rate_limits = validate_github_tokens(tokens, token_proxy_map)
    
    if not valid_tokens:
        print("Error: No valid GitHub tokens found. Please check your tokens.")
        return
    
    # Check if all tokens are rate limited using /rate_limit API
    print("\nChecking rate limit status for all tokens...")
    detailed_rate_limits = check_token_rate_limits(valid_tokens, token_proxy_map)
    
    # Check if all tokens are rate limited
    if check_all_tokens_rate_limited(valid_tokens, token_rate_limits):
        print("\n⚠️  WARNING: All tokens are rate limited!")
        print("=" * 60)
        print("Rate Limit Status for All Tokens:")
        print("=" * 60)
        
        current_time = time.time()
        for token in valid_tokens:
            token_mask = mask_token(token)
            
            # Get rate limit info from detailed check or validation
            if token in detailed_rate_limits:
                info = detailed_rate_limits[token]
                remaining = info.get('remaining', 0)
                limit = info.get('limit', 5000)
                reset_time = info.get('reset', 0)
            elif token in token_rate_limits:
                info = token_rate_limits[token]
                remaining = info.get('remaining', 0)
                limit = 5000
                reset_time = info.get('reset_time', 0)
            else:
                remaining = 0
                limit = 5000
                reset_time = 0
            
            # Calculate recovery time
            if reset_time > 0:
                recovery_seconds = max(0, reset_time - current_time)
                recovery_str = format_recovery_time(recovery_seconds)
                reset_datetime = datetime.fromtimestamp(reset_time).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  Token: {token_mask}")
                print(f"    Remaining: {remaining} / {limit}")
                print(f"    Reset time: {reset_datetime}")
                print(f"    Recovery time: {recovery_str}")
            else:
                print(f"  Token: {token_mask}")
                print(f"    Remaining: {remaining} / {limit}")
                print(f"    Reset time: Unknown")
                print(f"    Recovery time: Unknown")
            print()
        
        print("=" * 60)
        print("All tokens are rate limited. Please wait for tokens to recover before running again.")
        print("Exiting...")
        return
    
    num_workers = len(valid_tokens)
    
    # Calculate total available quota from all tokens
    total_quota = 0
    for token in valid_tokens:
        if token in token_rate_limits:
            quota = token_rate_limits[token].get('remaining', 5000)
            total_quota += quota
        else:
            total_quota += 5000  # Default quota if not available
    
    # Dynamic concurrency: calculate workers based on total available quota
    # Formula: total_quota / 1000, but ensure:
    # - At least 1 worker
    # - At most num_workers (don't exceed number of tokens)
    # - At least 2 workers if we have multiple tokens with good quota
    calculated_workers = max(1, min(num_workers, total_quota // 1000))
    
    # If we have multiple tokens with good quota, use at least 2 workers
    # This ensures better utilization of multiple tokens
    if num_workers >= 2 and total_quota >= 2000:
        calculated_workers = max(2, calculated_workers)
    
    # If all tokens are rate-limited (low quota), use fewer workers to avoid exhausting them
    if total_quota < 1000:
        calculated_workers = min(2, num_workers)
    
    # Cap workers_per_lang: GitHub Search API has a separate rate limit
    # (30 req/min per token) that doesn't scale with more tokens.
    # Too many workers causes Search API exhaustion and stalls.
    workers_per_lang = min(calculated_workers, 5)

    log(f"\n✓ {num_workers} valid token(s) found.")
    log(f"Total available quota: {total_quota}")
    log(f"Auto-setting workers_per_lang to {workers_per_lang} (calculated from quota: {total_quota} / 1000 = {total_quota // 1000}, max {num_workers}).\n")
    
    # Initialize components with valid tokens and their rate limit info
    valid_token_proxy_map = {
        token: token_proxy_map[token]
        for token in valid_tokens
        if token in token_proxy_map
    }
    token_manager = TokenManager(tokens=valid_tokens, initial_rate_limits=token_rate_limits)
    client = GitHubClient(token_manager, token_proxy_map=valid_token_proxy_map)
    
    # Setup output directory
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    
    progress_dir = os.path.join(output_dir, 'progress')
    progress = ProgressTracker(progress_dir, force_recheck_all=args.force_recheck_all)
    qualifying_tracker = QualifyingRepoTracker(progress_dir)
    
    # Initialize filtering statistics tracker
    filtering_stats = FilteringStatistics(output_dir)
    
    # Create language-specific output files
    def get_lang_prs_file(lang: str) -> str:
        """Get the prs.jsonl file path for a specific language."""
        return os.path.join(output_dir, f'{lang}_prs.jsonl')
    
    def get_lang_pr_ids_file(lang: str) -> str:
        """Get the pr_ids.txt file path for a specific language."""
        return os.path.join(output_dir, f'{lang}_pr_ids.txt')
    
    def get_lang_repos_file(lang: str) -> str:
        """Get the repos.jsonl file path for a specific language."""
        lang_dir = os.path.join(output_dir, lang)
        os.makedirs(lang_dir, exist_ok=True)
        return os.path.join(lang_dir, 'repos.jsonl')
    
    output_lock = Lock()
    
    # Determine languages to process
    if args.languages:
        languages = [l.strip().lower() for l in args.languages.split(',')]
    else:
        languages = LANGUAGES  # Process all languages sequentially

    dedupe_pr_id_files(output_dir, languages)
    
    # Check if progress bars should be disabled (useful for multi-process)
    use_progress_bars = TQDM_AVAILABLE and not args.disable_progress_bar
    
    # Get process position for tqdm (to avoid conflicts in multi-process environments)
    # Use environment variable TQDM_POSITION if set, otherwise use 0
    tqdm_position = int(os.environ.get('TQDM_POSITION', '0'))
    
    log(f"Processing languages: {languages} (parallel processing)")
    log(f"Target: {args.repo_num} repos with qualifying PRs per language")
    log(f"Max PRs per repo: {args.max_prs_per_repo}")
    if not use_progress_bars:
        log("Progress bars disabled (--disable_progress_bar flag)")
    elif tqdm_position > 0:
        log(f"Using tqdm position {tqdm_position} for multi-process progress bar")

    all_qualifying_prs = []
    all_qualifying_repos = []
    all_qualifying_lock = Lock()

    def _process_one_language(lang, lang_idx):
        """Process a single language end-to-end. Thread-safe."""
        nonlocal all_qualifying_prs, all_qualifying_repos

        existing_count = qualifying_tracker.get_qualifying_count(lang)
        if existing_count >= args.repo_num:
            log(f"[{lang}] Already have {existing_count}/{args.repo_num} qualifying repos. Skipping.")
            return

        search_target = get_candidate_search_target(existing_count, args.repo_num)
        log(
            f"[{lang}] Starting... (have {existing_count}/{args.repo_num} qualifying repos, "
            f"need {search_target} more)"
        )

        # Skip repos from candidate search:
        # 1. Already-qualifying repos (their PRs are already collected)
        # 2. Repos fully rechecked under the current criteria version that
        #    didn't qualify — no point re-scanning them again.
        # Repos processed only under OLDER criteria are NOT skipped so their
        # PRs can be re-evaluated against the (possibly relaxed) filters.
        already_qualifying = qualifying_tracker.get_qualifying_repos(lang)
        rechecked_non_qualifying = {
            repo for repo in progress.rechecked_repos
            if repo not in already_qualifying
        }
        skip_repos = already_qualifying | rechecked_non_qualifying
        log(
            f"[{lang}] skip_repos: {len(skip_repos)} "
            f"(qualifying={len(already_qualifying)}, "
            f"rechecked_non_qual={len(rechecked_non_qualifying)})"
        )

        # Repos previously processed (under any criteria version) already passed
        # all repo-level checks.  Since we only relax criteria, they still pass —
        # skip the expensive per-repo API calls (language %, CI, deps) for them.
        needs_pr_recheck = {
            repo for repo in progress.processed_prs
            if repo not in skip_repos and not progress.is_repo_rechecked(repo)
        }

        candidates, repo_stats = get_candidate_repos(
            client, lang,
            min_stars=get_lang_config(lang, 'MIN_STARS'),
            target_repo_count=args.repo_num,
            skip_full_names=skip_repos,
            fast_pass_repos=needs_pr_recheck,
            max_candidates_override=args.max_candidate_repos,
        )

        filtering_stats.init_language(lang)
        filtering_stats.update_repo_stats(
            lang,
            repos_searched=repo_stats['repos_searched'],
            repos_candidate=repo_stats['repos_candidate'],
            filter_reasons=repo_stats['filter_reasons']
        )

        candidates_to_process = [
            c for c in candidates
            if not qualifying_tracker.is_repo_qualifying(lang, c['full_name'])
        ]

        if not candidates_to_process:
            log(
                f"[{lang}] No new candidate repos found (skipped {len(skip_repos)} "
                f"already-processed repos). GitHub Search may be exhausted for current "
                f"star range / filters."
            )
            return

        log(f"[{lang}] {len(candidates_to_process)} new candidate repos to process")

        # Process repos one by one until we have enough qualifying repos
        qualifying_count_for_lang = existing_count
        total_candidates = len(candidates_to_process)

        # Parallel repo processing within each language (use 3 workers per language)
        # With 8 languages × 3 workers = 24 total threads, well within 99 token capacity
        repo_workers = min(workers_per_lang, len(candidates_to_process))

        def _process_single_repo(repo_info):
            """Process a single repo and return (prs, pr_stats, repo_info) or None."""
            nonlocal qualifying_count_for_lang
            if qualifying_count_for_lang >= args.repo_num:
                return None

            full_name = repo_info['full_name']
            lang_prs_file = get_lang_prs_file(lang)

            try:
                repo_was_qualifying = qualifying_tracker.is_repo_qualifying(lang, full_name)
                prs, pr_stats = process_repo_prs(
                    client=client,
                    repo_info=repo_info,
                    progress=progress,
                    output_lock=output_lock,
                    prs_file=lang_prs_file,
                    max_prs_per_repo=args.max_prs_per_repo,
                    repo_was_qualifying=repo_was_qualifying
                )
                time.sleep(REPO_COMPLETE_DELAY)
                return (prs, pr_stats, repo_info)
            except Exception:
                return None

        if repo_workers <= 1:
            # Fallback to sequential for very small candidate lists
            for repo_info in candidates_to_process:
                if qualifying_count_for_lang >= args.repo_num:
                    break
                result = _process_single_repo(repo_info)
                if result is None:
                    continue
                prs, pr_stats, ri = result
                filtering_stats.update_pr_stats(
                    lang,
                    prs_scanned=pr_stats['prs_scanned'],
                    prs_merged=pr_stats['prs_merged'],
                    prs_qualifying=pr_stats['prs_qualifying'],
                    filter_reasons=pr_stats['filter_reasons']
                )
                if len(prs) > 0:
                    qualifying_tracker.add_qualifying_repo(lang, ri['full_name'])
                    qualifying_count_for_lang += 1
                    with all_qualifying_lock:
                        all_qualifying_prs.extend(prs)
                        all_qualifying_repos.append(ri)
                    filtering_stats.update_repo_stats(lang, repos_with_qualifying_prs=1)
                    lang_repos_file = get_lang_repos_file(lang)
                    with output_lock:
                        with open(lang_repos_file, 'a') as f:
                            ri['qualifying_pr_count'] = len(prs)
                            f.write(json.dumps(ri, ensure_ascii=False) + '\n')
        else:
            from concurrent.futures import ThreadPoolExecutor as RepoTPE
            with RepoTPE(max_workers=repo_workers) as repo_executor:
                future_to_repo = {}
                submitted = 0
                for repo_info in candidates_to_process:
                    if qualifying_count_for_lang >= args.repo_num:
                        break
                    fut = repo_executor.submit(_process_single_repo, repo_info)
                    future_to_repo[fut] = repo_info
                    submitted += 1

                for fut in as_completed(future_to_repo):
                    if qualifying_count_for_lang >= args.repo_num:
                        break
                    result = fut.result()
                    if result is None:
                        continue
                    prs, pr_stats, ri = result
                    filtering_stats.update_pr_stats(
                        lang,
                        prs_scanned=pr_stats['prs_scanned'],
                        prs_merged=pr_stats['prs_merged'],
                        prs_qualifying=pr_stats['prs_qualifying'],
                        filter_reasons=pr_stats['filter_reasons']
                    )
                    if len(prs) > 0:
                        qualifying_tracker.add_qualifying_repo(lang, ri['full_name'])
                        qualifying_count_for_lang += 1
                        with all_qualifying_lock:
                            all_qualifying_prs.extend(prs)
                            all_qualifying_repos.append(ri)
                        filtering_stats.update_repo_stats(lang, repos_with_qualifying_prs=1)
                        lang_repos_file = get_lang_repos_file(lang)
                        with output_lock:
                            with open(lang_repos_file, 'a') as f:
                                ri['qualifying_pr_count'] = len(prs)
                                f.write(json.dumps(ri, ensure_ascii=False) + '\n')

        log(f"[{lang}] Completed: {qualifying_count_for_lang}/{args.repo_num} qualifying repos")

    # Process all languages in parallel using threads
    from concurrent.futures import ThreadPoolExecutor as LangTPE
    max_lang_threads = min(len(languages), 8)  # All 8 languages in parallel (99 tokens available)
    log(f"Launching {max_lang_threads} language threads for {len(languages)} languages")

    with LangTPE(max_workers=max_lang_threads) as lang_executor:
        futures = {
            lang_executor.submit(_process_one_language, lang, idx): lang
            for idx, lang in enumerate(languages)
        }
        for future in as_completed(futures):
            lang_name = futures[future]
            try:
                future.result()
            except Exception as e:
                log(f"[{lang_name}] Error: {e}")

    
    # Summary (only if processing multiple languages or not in multi-process mode)
    if len(languages) > 1:
        log(f"\n{'='*60}")
        log("SUMMARY")
        log(f"{'='*60}")
        log(f"Total qualifying repos: {len(all_qualifying_repos)}")
        log(f"Total qualifying PRs: {len(all_qualifying_prs)}")
    else:
        # Single language: show summary for that language
        lang = languages[0]
        lang_count = qualifying_tracker.get_qualifying_count(lang)
        log(f"\n{'='*60}", lang=lang)
        log(f"SUMMARY for {lang}", lang=lang)
        log(f"{'='*60}", lang=lang)
        log(f"Qualifying repos: {lang_count}/{args.repo_num}", lang=lang)
    
    log(f"Output directory: {output_dir}")
    log(f"  - Progress: {progress_dir}/")
    log(f"  - Statistics: {os.path.join(output_dir, 'filtering_statistics.json')}")
    
    # Save filtering statistics report
    filtering_stats.save_summary_report()

    # Flush any remaining unsaved progress
    progress._save_progress()
    progress.save_rechecked_repos()

    # Print filtering statistics summary
    stats_summary = filtering_stats.get_summary()
    log(f"\n{'='*60}")
    log("FILTERING STATISTICS")
    log(f"{'='*60}")
    
    totals = stats_summary['totals']
    log(f"Repos: {totals['repos_searched']} searched -> {totals['repos_candidate']} candidates ({totals['repo_candidate_rate']}) -> {totals['repos_with_qualifying_prs']} with qualifying PRs ({totals['repo_qualifying_rate']})")
    log(f"PRs: {totals['prs_scanned']} scanned -> {totals['prs_merged']} merged ({totals['pr_merge_rate']}) -> {totals['prs_qualifying']} qualifying ({totals['pr_qualifying_rate']})")
    
    # Per-language summary
    log("\nQualifying repos per language:")
    for lang in languages:
        count = qualifying_tracker.get_qualifying_count(lang)
        lang_stats = stats_summary['languages'].get(lang, {})
        searched = lang_stats.get('repos_searched', 0)
        candidates = lang_stats.get('repos_candidate', 0)
        log(f"  {lang}: {count} repos (searched: {searched}, candidates: {candidates})")
    
    lang_pr_counts = {}
    for pr in all_qualifying_prs:
        lang = pr['language']
        lang_pr_counts[lang] = lang_pr_counts.get(lang, 0) + 1
    
    log("\nQualifying PRs per language:")
    for lang in sorted(set(list(lang_pr_counts.keys()) + languages)):
        count = lang_pr_counts.get(lang, 0)
        lang_stats = stats_summary['languages'].get(lang, {})
        scanned = lang_stats.get('prs_scanned', 0)
        merged = lang_stats.get('prs_merged', 0)
        log(f"  {lang}: {count} PRs (scanned: {scanned}, merged: {merged})")
    
    log(f"\nFiltering report saved to: {os.path.join(output_dir, 'filtering_report.md')}")


if __name__ == "__main__":
    main()
