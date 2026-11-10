"""Guard: tracked files must not carry personal or private-infrastructure strings."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built from fragments so this file does not match its own patterns.
FORBIDDEN = [
    "mag" + "cloud",
    "rm" + "agno",
    "rod" + "rigo",
    "rom" + "agn",
    "jar" + "vis",
    "mag" + "no",
    "un" + "raid",
    "recon" + "verge",
    "ai-work-" + "organizer",
    r"192\." + r"168\.",
    r"\bfam" + r"ily\b",
]
PATTERN = re.compile("|".join(FORBIDDEN), re.IGNORECASE)
SKIP = {"tests/test_no_private_strings.py", "poetry.lock"}

# The project's public repository. These exact strings are removed before
# scanning, so the owner's account name is allowed there and nowhere else
# (not in a path, an e-mail address, a container registry or an example).
ALLOWED_EXACT = ["github.com/" + "rom" + "agn/memsilo"]


def private_hits(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every line of `text` with a forbidden string."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        scrubbed = line
        for allowed in ALLOWED_EXACT:
            scrubbed = scrubbed.replace(allowed, "")
        if PATTERN.search(scrubbed):
            hits.append((n, line))
    return hits


def test_tracked_files_have_no_private_strings():
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    hits = []
    for rel in out:
        if rel in SKIP:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in private_hits(text):
            hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "private strings found:\n" + "\n".join(hits)


_OWNER = "rom" + "agn"


def test_only_the_exact_repository_url_may_name_the_owner():
    ok = f"git clone https://github.com/{_OWNER}/memsilo.git\n[r](https://github.com/{_OWNER}/memsilo#readme)"
    assert private_hits(ok) == []
    for bad in (
        f"image: ghcr.io/{_OWNER}/x",
        f"contact {_OWNER}@example.com",
        f"/home/{_OWNER}/memsilo",
        f"https://github.com/{_OWNER}/other-repo",
        f"https://github.com/{_OWNER}/memsilo and also {_OWNER}",
        "r" + "magno",
        "rod" + "rigo." + "mag" + "no",
    ):
        assert private_hits(bad), bad


ALLOWED_DOTFILES = {
    ".gitignore",
    ".dockerignore",
    ".env.example",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    "data/.gitkeep",
}


def test_only_allowlisted_dotfiles_are_tracked():
    """No `.env`, `.claude/`, `.superpowers/`, editor or tool state in git."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = [
        rel
        for rel in out
        if any(part.startswith(".") for part in rel.split("/")) and rel not in ALLOWED_DOTFILES
    ]
    assert not offenders, "dot-paths tracked outside the allowlist:\n" + "\n".join(offenders)


# Wording from the project's single-operator origins that reads wrong (or
# hints at a private setup) in a multi-user public release.
ORIGIN_WORDING = re.compile(
    r"\(owner\)|owner's own user|on this machine|exactly as before this server", re.IGNORECASE
)


def test_no_single_operator_origin_wording():
    out = subprocess.run(
        ["git", "ls-files", "*.py", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    hits = []
    for rel in out:
        if rel == "tests/test_no_private_strings.py":
            continue
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if ORIGIN_WORDING.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "origin wording found:\n" + "\n".join(hits)
