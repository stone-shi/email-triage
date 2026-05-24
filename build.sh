#!/bin/bash

# Exit on error, undefined variables, and pipe failures
set -euo pipefail

# Color codes for pretty printing
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0;30m' # No Color
CLEAR='\033[0m'

log_info() {
    echo -e "${BLUE}[INFO]${CLEAR} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${CLEAR} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${CLEAR} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${CLEAR} $1" >&2
}

# Resolve directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# Configuration variables
IMAGE_NAME="email-triage"
REGISTRY="registry.shifamily.com"
REPOSITORY="homestack"
TAG="latest"

# Parse CLI options (e.g. custom tag)
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--tag)
            TAG="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  -t, --tag <tag_name>   Specify target Docker tag (default: latest)"
            echo "  -h, --help             Show this help menu"
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            exit 1
            ;;
    esac
done

TARGET_IMAGE="${REGISTRY}/${REPOSITORY}/${IMAGE_NAME}:${TAG}"

# Check if docker is installed
if ! command -v docker &> /dev/null; then
    log_error "Docker command not found. Please install Docker before running this script."
    exit 1
fi

# Check if Dockerfile exists
if [[ ! -f "Dockerfile" ]]; then
    log_error "Dockerfile not found in current directory (${SCRIPT_DIR})."
    exit 1
fi

log_info "Generating version.txt..."
GIT_REV=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "${GIT_REV} build: ${TIMESTAMP}" > version.txt

log_info "Building local Docker image '${IMAGE_NAME}:latest'..."
docker build -t "${IMAGE_NAME}:latest" -f Dockerfile .

log_info "Tagging image as '${TARGET_IMAGE}'..."
docker tag "${IMAGE_NAME}:latest" "${TARGET_IMAGE}"

log_info "Pushing image '${TARGET_IMAGE}' to registry..."
docker push "${TARGET_IMAGE}"

log_success "Docker image successfully built, tagged, and pushed to ${TARGET_IMAGE}!"
