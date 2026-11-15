#!/usr/bin/env bash
# Purge rendered chapter videos from git history.
#
# Why: HEAD no longer tracks them (they are gitignored), but every earlier commit
# still carries ~1.79 GB of MP4 blobs, so the repo measures ~5.9 GB and clone /
# checkout stay expensive. This rewrites history to drop only those paths.
#
# Requires: a full clone of ~5.9 GB (run it somewhere with real bandwidth, e.g. a
# cloud box), git-filter-repo, and an account that can force-push main.
#
#   pip install --user git-filter-repo     # or: pipx install git-filter-repo
#   WORKDIR=/tmp/purge ./scripts/purge-video-history.sh
#
# Afterwards: every clone must be re-cloned (existing clones are now divergent),
# and open PRs against the old history need to be recreated.
#
# Caveats worth knowing before you run it:
#   * The one existing fork keeps the old history. Delete or re-fork it, or the
#     blobs stay reachable through that fork.
#   * GitHub serves unreachable objects by SHA for a while after a force-push. For
#     a public repo, ask support to run gc if the old blobs must really be gone.
#   * The Actions cache and artifacts are unaffected.

set -euo pipefail

REPO="${REPO:-https://github.com/charoenwitkunnna-blip/recapper.git}"
BRANCH="${BRANCH:-main}"
WORKDIR="${WORKDIR:-$PWD/recapper-purge}"

command -v git-filter-repo >/dev/null 2>&1 || {
  echo "git-filter-repo not found. Install it: pip install --user git-filter-repo" >&2
  exit 1
}

mkdir -p "$WORKDIR"
cd "$WORKDIR"

if [ ! -d mirror.git ]; then
  echo "==> Mirror clone (this downloads the full history, ~5.9 GB)"
  git clone --mirror "$REPO" mirror.git
fi

cd mirror.git
before=$(du -sh . | cut -f1)

echo "==> Removing */chapters/*/video.mp4 from all history"
git filter-repo --force --invert-paths --path-glob '*/chapters/*/video.mp4'

echo "==> Repacking"
git reflog expire --expire=now --all
git gc --prune=now --aggressive

after=$(du -sh . | cut -f1)
echo "==> Size: $before -> $after"

echo
echo "Sanity check (expect 0):"
git log --all --oneline -- '*chapters*video.mp4' | wc -l

read -r -p "Force-push rewritten history to $REPO ($BRANCH)? [y/N] " reply
case "$reply" in
  [yY]*) git push --force --mirror origin ;;
  *) echo "Aborted; nothing pushed. Mirror left at $WORKDIR/mirror.git" ;;
esac
