#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
REPO=$(CDPATH= cd -- "$ROOT/.." && pwd)
FALLBACK=0.0.0.dev0+unknown

command -v git >/dev/null 2>&1 || {
  echo "$FALLBACK"
  exit 0
}
git -C "$REPO" rev-parse --verify HEAD >/dev/null 2>&1 || {
  echo "$FALLBACK"
  exit 0
}

tags=$(git -C "$REPO" tag --points-at HEAD --list 'server/v*')
tag_count=$(printf '%s\n' "$tags" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$tag_count" -gt 1 ]; then
  echo "multiple server release tags point at HEAD" >&2
  exit 1
fi

dirty=
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=normal)" ]; then
  dirty=.dirty
fi

if [ "$tag_count" -eq 1 ] && [ -z "$dirty" ]; then
  version=${tags#server/v}
  printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || {
    echo "invalid server release tag: $tags" >&2
    exit 1
  }
  echo "$version"
  exit 0
fi

date=$(git -C "$REPO" show -s --format=%cs HEAD | tr -d '-')
sha=$(git -C "$REPO" rev-parse --short=12 HEAD)
echo "0.0.0.dev$date+g$sha$dirty"
