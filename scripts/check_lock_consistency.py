"""Verify the dependency lock files are consistent with their sources.

Why this exists
---------------
``requirements.txt`` states what we want (``>=`` ranges); the ``*.lock.txt`` files
state what is actually installed, and they are what CI and the Docker image
install from. Editing only ``requirements.txt`` therefore leaves CI building with
the *old* dependency set -- the change appears to do nothing, and nobody notices
until the behaviour differs between machines.

This check fails when:

1. A direct requirement in ``requirements.txt`` is missing from the runtime lock
   (added without regenerating).
2. A lock file pins a package that the source no longer declares at all
   (removed without regenerating).
3. The GPU dependency tree leaked in, which would undo the CPU-only image
   design that ``Dockerfile`` relies on.

Version *downgrades* and upstream releases are deliberately out of scope: a lock
file is allowed to be newer than its source, and bumping a pinned version is a
reviewed diff, not a mistake.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A pin looks like ``name[extras]==version``; uv emits the extras form whenever a
# requirement carries extras (``uvicorn[standard]==0.54.0``), so the extras
# suffix must be tolerated or genuinely-pinned packages look missing.
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")
GPU_PREFIXES = ("nvidia-", "cuda-", "triton")
EXACT_PINS = {"torch", "torchvision", "torchaudio"}


def normalize(name: str) -> str:
    """PEP 503 normalisation so ``Foo.Bar`` == ``foo-bar``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, str]:
    """Return {normalised_name: version} for every ``name==version`` pin."""
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Continuation lines of a ``# via`` comment block are indented.
        if raw.startswith((" ", "\t")):
            continue
        match = PIN_RE.match(line)
        if match:
            pins[normalize(match.group(1))] = match.group(2)
    return pins


def parse_lock_mentions(path: Path) -> set[str]:
    """Return every package name the lock file mentions, pinned or not.

    A direct requirement can be satisfied *transitively* -- ``uvicorn`` is
    declared in ``requirements.txt`` but uv emits it as a dependency of chromadb
    rather than as a top-level pin. Either way the version is frozen, which is
    all this check cares about.
    """
    mentions: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(raw.strip())
        if match:
            mentions.add(normalize(match.group(1)))
    return mentions


def parse_requirements(path: Path) -> list[str]:
    """Return the declared distribution names, ignoring comments and includes."""
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            # ``-r other.txt`` is resolved transitively by the compiler.
            continue
        name = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].strip()
        if name:
            names.append(normalize(name))
    return names


def check_pair(source_name: str, lock_name: str) -> list[str]:
    """Return a list of problem descriptions for one source/lock pair."""
    problems: list[str] = []
    source = ROOT / source_name
    lock = ROOT / lock_name
    if not lock.exists():
        return [f"{lock_name} is missing; run scripts/regenerate_lock.sh"]

    declared = parse_requirements(source)
    pinned = parse_lock(lock)
    mentioned = parse_lock_mentions(lock)

    for name in declared:
        if name not in mentioned:
            problems.append(
                f"{lock_name} does not pin '{name}' declared in {source_name}; "
                f"run scripts/regenerate_lock.sh"
            )

    leaked = sorted(n for n in pinned if n.startswith(GPU_PREFIXES))
    if leaked:
        problems.append(
            f"{lock_name} contains GPU packages ({', '.join(leaked)}); "
            f"the runtime image is CPU-only. Add them to requirements-lock-exclusions.txt "
            f"and regenerate."
        )

    for name in EXACT_PINS:
        if name in pinned:
            problems.append(
                f"{lock_name} pins '{name}'; it is installed by the Dockerfile from "
                f"the PyTorch CPU index and must stay out of the lock file."
            )

    return problems


def main() -> int:
    pairs = [
        ("requirements.txt", "requirements.lock.txt"),
        ("requirements-dev.txt", "requirements-dev.lock.txt"),
    ]
    all_problems: list[str] = []
    for source_name, lock_name in pairs:
        all_problems.extend(check_pair(source_name, lock_name))

    if all_problems:
        print("Dependency lock check FAILED:\n")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1

    runtime = len(parse_lock(ROOT / "requirements.lock.txt"))
    print(f"Dependency lock check passed ({runtime} runtime packages pinned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())