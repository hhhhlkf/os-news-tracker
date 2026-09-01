#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <python-3.12-base@sha256:digest> <registry-repository>" >&2
  exit 2
fi

base_image=$1
image_repository=$2
case "$base_image" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "base image must be pinned by sha256 digest" >&2; exit 2 ;;
esac
case "$image_repository" in
  *@sha256:*) echo "pass a repository name, not an existing digest reference" >&2; exit 2 ;;
esac

docker build --pull=false \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "RUNTIME_VERSION=openhands-agent-runtime:1.43.1-r2" \
  --tag "$image_repository:1.43.1-r2" \
  --file discovery-agent-runtime/Dockerfile .

echo "Record the reviewed sha256 image reference under openhands-agent-runtime:1.43.1-r2 in DISCOVERY_SANDBOX_RUNTIME_IMAGES."
