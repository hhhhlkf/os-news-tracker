#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <base-image@sha256:digest> <registry-repository> <crawler-runtime:version>" >&2
  exit 2
fi

base_image=$1
image_repository=$2
runtime_version=$3

case "$base_image" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "base image must be pinned by sha256 digest" >&2; exit 2 ;;
esac

case "$runtime_version" in
  crawler-runtime:*) ;;
  *) echo "runtime version must use crawler-runtime:<version>" >&2; exit 2 ;;
esac

case "$image_repository" in
  *@sha256:*) echo "pass a repository name, not an existing digest reference" >&2; exit 2 ;;
esac

version=${runtime_version#crawler-runtime:}
docker build \
  --pull=false \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "RUNTIME_VERSION=$runtime_version" \
  --tag "$image_repository:$version" \
  --file crawler-runtime/Dockerfile \
  .

echo "Push the reviewed tag to an internal or loopback-only development registry, record its sha256 digest in DISCOVERY_SANDBOX_RUNTIME_IMAGES, then deploy."
