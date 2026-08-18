import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Upstream brand names (SWE-gen / swegen) are intentionally allowed: the README
# credits abundant-ai/SWE-gen for Apache-2.0 attribution.
DISALLOWED_TEXT = (
    "SWE-Lego-" + "Live",
    "/gpu" + "fs/",
    "/home/" + "haoli",
    "ywx" + "zml3j",
)

DISALLOWED_PATTERNS = (
    re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}(?:\.\d{1,3}){2}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d+)?"
    ),
)


def tracked_text_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
    )
    return [
        ROOT / item.decode()
        for item in output.split(b"\0")
        if item and (ROOT / item.decode()).is_file()
    ]


def test_tracked_files_are_publication_safe() -> None:
    violations: list[str] = []

    for path in tracked_text_files():
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue

        relative = path.relative_to(ROOT)
        for marker in DISALLOWED_TEXT:
            if marker in content:
                violations.append(f"{relative}: contains disallowed marker {marker!r}")
        for pattern in DISALLOWED_PATTERNS:
            if pattern.search(content):
                violations.append(f"{relative}: matches {pattern.pattern!r}")

    assert not violations, "\n".join(violations)
