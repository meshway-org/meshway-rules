#!/usr/bin/env bash
# Usage: scripts/push-dist.sh OUT REMOTE PREVIOUS_COMMIT
#
# Commits the signed tree OUT as a parentless commit, then atomically
# force-pushes it to `dist` and pushes the tag `catalog-<sequence>`.
# PREVIOUS_COMMIT is the `dist` commit the build started from (empty when
# there was none); the push fails if `dist` moved meanwhile.
set -euo pipefail

out=$1 remote=$2 previous=$3
sequence=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["sequence"])' "$out/index.json")
git_dir=$(git rev-parse --absolute-git-dir)
index_dir=$(mktemp -d)
trap 'rm -rf "$index_dir"' EXIT
export GIT_INDEX_FILE="$index_dir/index"

git -C "$out" --git-dir="$git_dir" --work-tree=. add -A .
tree=$(git --git-dir="$git_dir" write-tree)
commit=$(git --git-dir="$git_dir" commit-tree "$tree" -m "catalog $sequence")
git tag "catalog-$sequence" "$commit"
git push --atomic --force-with-lease="refs/heads/dist:$previous" "$remote" \
  "$commit:refs/heads/dist" "refs/tags/catalog-$sequence"
echo "catalog-$sequence $commit"
