#!/usr/bin/env bash
# Builds a real, signed apt repository from a directory of .deb files --
# no reprepro/aptly dependency, just dpkg-scanpackages + apt-ftparchive
# (both standard dpkg-dev/apt-utils tools, already present on any
# Debian/Ubuntu box, including GitHub's ubuntu-latest runners) plus gpg.
#
# GitHub Packages has no native apt/.deb repository type (checked, not
# assumed -- see plans/deployment-and-provisioning-decisions.md), so this
# is what actually gets published to GitHub Pages: a plain static file
# tree (Packages/Release/InRelease + a pool/ of .debs) served over HTTPS,
# the same shape any apt repo is under the hood.
#
# Usage: publish-apt-repo.sh <deb-dir> <output-dir> <public-key-armor-file> [gpg-key-id] [pages-url]
# gpg-key-id is optional -- if omitted, gpg falls back to "the only secret
# key in the keyring", which is correct right after a fresh --import of
# exactly our one signing key, but passing it explicitly (matches
# KRAUKEN_GPG_KEY_ID in the workflow) avoids ever depending on that being
# implicitly true. pages-url is optional too -- fills in the generated
# index.html's install instructions with the real, copy-pasteable URL
# instead of a <this-pages-url> placeholder.
set -euo pipefail

DEB_DIR="${1:?usage: publish-apt-repo.sh <deb-dir> <output-dir> <public-key-armor-file> [gpg-key-id] [pages-url]}"
OUT_DIR="${2:?}"
PUBKEY_FILE="${3:?}"
GPG_KEY_ID="${4:-}"
PAGES_URL="${5:-<this-pages-url>}"
GPG_USER_ARGS=()
if [ -n "$GPG_KEY_ID" ]; then
    GPG_USER_ARGS=(--local-user "$GPG_KEY_ID")
fi

DISTRIBUTION="stable"
COMPONENT="main"
ARCH="all" # matches debian/control's Architecture: all (source-shipped, see its long description)

POOL_DIR="${OUT_DIR}/pool/${COMPONENT}"
DISTS_DIR="${OUT_DIR}/dists/${DISTRIBUTION}"
BINARY_DIR="${DISTS_DIR}/${COMPONENT}/binary-${ARCH}"

mkdir -p "$POOL_DIR" "$BINARY_DIR"
cp "${DEB_DIR}"/*.deb "$POOL_DIR"/

# Packages index -- paths inside it are relative to $OUT_DIR (the repo
# root), matching what a `deb ... stable main` sources.list entry expects
# to resolve Filename: against.
( cd "$OUT_DIR" && dpkg-scanpackages --arch "$ARCH" "pool/${COMPONENT}" > "dists/${DISTRIBUTION}/${COMPONENT}/binary-${ARCH}/Packages" )
gzip -9c "${BINARY_DIR}/Packages" > "${BINARY_DIR}/Packages.gz"

# Release -- describes what's in dists/stable/ (which components/
# architectures exist, plus checksums of every Packages file) so apt
# knows what to trust and fetch.
cat > "${DISTS_DIR}/aptftp.conf" <<EOF
APT::FTPArchive::Release::Origin "TheKrauken";
APT::FTPArchive::Release::Label "Krauken";
APT::FTPArchive::Release::Suite "${DISTRIBUTION}";
APT::FTPArchive::Release::Codename "${DISTRIBUTION}";
APT::FTPArchive::Release::Components "${COMPONENT}";
APT::FTPArchive::Release::Architectures "${ARCH}";
EOF
( cd "$DISTS_DIR" && apt-ftparchive -c aptftp.conf release . > Release )
rm "${DISTS_DIR}/aptftp.conf"

# Sign -- both forms, for compatibility with every apt version:
# Release.gpg (detached, the legacy form) and InRelease (clearsigned,
# what modern apt actually prefers and checks first).
gpg --batch --yes "${GPG_USER_ARGS[@]}" --detach-sign --armor --output "${DISTS_DIR}/Release.gpg" "${DISTS_DIR}/Release"
gpg --batch --yes "${GPG_USER_ARGS[@]}" --clearsign --output "${DISTS_DIR}/InRelease" "${DISTS_DIR}/Release"

cp "$PUBKEY_FILE" "${OUT_DIR}/krauken-archive-keyring.asc"

cat > "${OUT_DIR}/index.html" <<EOF
<!doctype html>
<title>Krauken apt repository</title>
<pre>
Add this repository, then install normally:

  curl -fsSL ${PAGES_URL}/krauken-archive-keyring.asc \\
    | sudo gpg --dearmor -o /usr/share/keyrings/krauken-archive-keyring.gpg

  echo "deb [signed-by=/usr/share/keyrings/krauken-archive-keyring.gpg] ${PAGES_URL} stable main" \\
    | sudo tee /etc/apt/sources.list.d/krauken.list

  sudo apt update && sudo apt install krauken
</pre>
EOF

echo "Published apt repo to ${OUT_DIR}:"
find "$OUT_DIR" -type f | sort
