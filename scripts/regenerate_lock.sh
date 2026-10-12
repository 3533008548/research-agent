#!/usr/bin/env bash
# Regenerate the dependency lock files.
#
# Run this after ANY edit to requirements.txt / requirements-dev.txt, and commit
# the regenerated *.lock.txt together with the change. CI and the Docker image
# install from the lock files only, so a stale lock silently ignores your edit.
#
# Requires: uv  (https://docs.astral.sh/uv/)
#
# --python-version / --python-platform pin the *resolution target* to what CI
# actually runs (python:3.11-slim on linux). Without them the resolver picks
# versions for the host (Windows) instead.
#
# --exclude-newer freezes the resolution time point so re-running this script
# later does not silently upgrade anything. Bump it deliberately, review the
# diff, then re-run the retrieval eval.

set -euo pipefail

cd "$(dirname "$0")/.."

# Resolve the uv binary: $UV wins, else PATH. The explicit override matters
# because uv is often installed into a throwaway venv rather than system-wide.
UV="${UV:-uv}"
command -v "$UV" >/dev/null 2>&1 || [ -x "$UV" ] || {
  echo "error: uv not found." >&2
  echo "  Install:      pip install uv" >&2
  echo "  Or override:  UV=/path/to/uv $0" >&2
  exit 1
}

# torch and its GPU tree are installed by the Dockerfile from the PyTorch CPU
# index; see requirements-lock-exclusions.txt for the full rationale.
mapfile -t EXCLUDED < <(grep -vE '^\s*(#|$)' requirements-lock-exclusions.txt)
EXCLUDE_ARGS=()
for pkg in "${EXCLUDED[@]}"; do
  EXCLUDE_ARGS+=(--no-emit-package "$pkg")
done

echo "==> Regenerating requirements.lock.txt ($((${#EXCLUDED[@]} + 1)) packages excluded)"
"$UV" pip compile requirements.txt \
  --python-version 3.11 \
  --python-platform x86_64-manylinux_2_28 \
  --no-strip-extras \
  --exclude-newer 2026-10-12 \
  "${EXCLUDE_ARGS[@]}" \
  --output-file requirements.lock.txt

echo "==> Regenerating requirements-dev.lock.txt"
"$UV" pip compile requirements-dev.txt \
  --python-version 3.11 \
  --python-platform x86_64-manylinux_2_28 \
  --no-strip-extras \
  --exclude-newer 2026-10-12 \
  "${EXCLUDE_ARGS[@]}" \
  --output-file requirements-dev.lock.txt

echo "==> Sanity check: no GPU packages leaked into the lock files"
if grep -qE '^(nvidia-|cuda-|triton|torch==)' requirements.lock.txt requirements-dev.lock.txt; then
  echo "error: GPU packages present in lock file. Check requirements-lock-exclusions.txt" >&2
  grep -E '^(nvidia-|cuda-|triton|torch==)' requirements.lock.txt requirements-dev.lock.txt >&2
  exit 1
fi

echo "==> Done. Review the diff before committing."