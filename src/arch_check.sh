#!/usr/bin/env bash
# 依赖方向校验：违反 docs/02-开发规范.md 第 2 节即失败。
set -euo pipefail

fail=0

check_no_import() {
  local pkg="$1" forbidden="$2"
  if go list -f '{{join .Imports "\n"}}' "./$pkg" 2>/dev/null | grep -qx "$forbidden"; then
    echo "ARCH VIOLATION: $pkg must not import $forbidden"
    fail=1
  fi
}

check_only_stdlib() {
  local pkg="$1"
  local bad
  bad=$(go list -f '{{join .Imports "\n"}}' "./$pkg" 2>/dev/null \
        | grep '\.' | grep -v '^yktops/' || true)
  if [ -n "$bad" ]; then
    echo "ARCH VIOLATION: $pkg must be stdlib-only, found: $bad"
    fail=1
  fi
}

check_only_stdlib internal/domain

for p in internal/ykt/protocol internal/ykt/profile internal/store internal/queue internal/ykt; do
  check_no_import "$p" "yktops/internal/httpx"
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "arch check ok"
