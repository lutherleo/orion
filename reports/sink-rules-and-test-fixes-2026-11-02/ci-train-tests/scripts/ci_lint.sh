#!/bin/bash
# actionlint on the committed workflow, read from the shared .git (no tracked file is touched).
set -u
W=$(mktemp -d /tmp/ci-lint.XXXXXX)
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
  git clone -q --depth 1 --branch ci/run-train-tests "file:///mnt/c/Users/h/Downloads/orion/.git" "$W/repo" || exit 2
cd "$W/repo" || exit 2
uvx --from actionlint-py actionlint -version
uvx --from actionlint-py actionlint -verbose .github/workflows/tests.yml 2>&1 | tail -8
echo "actionlint exit=${PIPESTATUS[0]}"
rm -rf "$W"
