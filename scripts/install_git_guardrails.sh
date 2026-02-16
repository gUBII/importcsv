#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

chmod +x "$ROOT_DIR/.githooks/pre-commit" "$ROOT_DIR/.githooks/pre-push"
git -C "$ROOT_DIR" config core.hooksPath .githooks

echo "Installed git guardrails:"
echo "  - .githooks/pre-commit"
echo "  - .githooks/pre-push"
echo "core.hooksPath=.githooks"

