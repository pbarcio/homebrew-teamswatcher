#!/bin/bash
# Publishes a new version to your Homebrew tap.
#   ./release.sh 0.2.0
#
# Tags the current commit, pushes it, and points Formula/teamswatcher.rb at the new
# release tarball (URL + sha256). Coworkers then get it with `brew upgrade teamswatcher`.
set -euo pipefail
cd "$(dirname "$0")"

die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

version="${1:-}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Usage: ./release.sh <version>, e.g. ./release.sh 0.2.0"
tag="v$version"

git rev-parse --git-dir >/dev/null 2>&1 || die "This folder isn't a git repo yet. Push it to GitHub first (see Publishing in the README)."
git remote get-url origin >/dev/null 2>&1 || die "No 'origin' remote. Create the GitHub repo and push to it first (see Publishing in the README)."
[[ -z "$(git status --porcelain)" ]] || die "Commit or stash your changes first."
git rev-parse "$tag" >/dev/null 2>&1 && die "Tag $tag already exists."

remote="$(git remote get-url origin)"
if [[ "$remote" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
  owner="${BASH_REMATCH[1]}"
  repo="${BASH_REMATCH[2]}"
else
  die "origin isn't a GitHub repo: $remote"
fi
[[ "$repo" == "homebrew-teamswatcher" ]] || die "The repo must be named homebrew-teamswatcher for 'brew tap $owner/teamswatcher' to work (found $repo)."

branch="$(git rev-parse --abbrev-ref HEAD)"
git tag "$tag"
git push origin "$branch" "$tag"

url="https://github.com/$owner/$repo/archive/refs/tags/$tag.tar.gz"
echo "Fetching $url to compute its checksum..."
sha=""
for attempt in 1 2 3 4 5; do
  if sha="$(curl -fsSL "$url" | shasum -a 256 | cut -d' ' -f1)" && [[ -n "$sha" ]]; then break; fi
  sleep 3
done
[[ -n "$sha" ]] || die "Couldn't download $url"

sed -i '' -E \
  -e "s|github\.com/OWNER/|github.com/$owner/|g" \
  -e "s|^  url \".*\"|  url \"$url\"|" \
  -e "s|^  sha256 \".*\"|  sha256 \"$sha\"|" \
  Formula/teamswatcher.rb

git commit -am "teamswatcher $version"
git push origin "$branch"

echo
echo "Released $tag."
echo "Install:  brew install $owner/teamswatcher/teamswatcher && teamswatcher-setup"
echo "Upgrade:  brew upgrade teamswatcher && teamswatcher-setup"
