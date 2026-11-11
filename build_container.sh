#!/usr/bin/env bash
# Builds the SeedView image, tagged with both a version and `latest`.
#
# Usage:
#   ./build_container.sh            # version = git describe (falls back to short SHA)
#   ./build_container.sh 1.4.0      # explicit version tag
#
# Push separately once you're happy with a build, e.g.:
#   docker push myregistry/seedview:1.4.0
#   docker push myregistry/seedview:latest
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-seedview_app}"
VERSION="${1:-$(git describe --tags --always --dirty 2>/dev/null || echo "dev")}"

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Building ${IMAGE_NAME}:${VERSION} and ${IMAGE_NAME}:latest..."
docker build -t "${IMAGE_NAME}:${VERSION}" -t "${IMAGE_NAME}:latest" .

echo
echo "Built:"
docker images "${IMAGE_NAME}" --format "  {{.Repository}}:{{.Tag}}   {{.ID}}   {{.CreatedSince}}"
