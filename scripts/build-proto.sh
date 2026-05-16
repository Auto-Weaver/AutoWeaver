#!/usr/bin/env bash
# Regenerate Python gRPC stubs from proto/motion.proto.
#
# Run this after any change to proto/motion.proto. The generated stubs
# (motion_pb2.py, motion_pb2_grpc.py) are committed into the repo so
# downstream users don't need protoc installed.
#
# Usage:
#   ./scripts/build-proto.sh
#
# Requires: grpcio-tools (already in pyproject.toml dependency-groups.dev).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="${REPO_ROOT}/proto"
OUT_DIR="${REPO_ROOT}/src/autoweaver/motion_policy/_proto"

mkdir -p "${OUT_DIR}"

# Ensure the output directory is a package.
if [[ ! -f "${OUT_DIR}/__init__.py" ]]; then
    cat > "${OUT_DIR}/__init__.py" <<'EOF'
"""Generated gRPC stubs for the motion-runtime proto.

Do not edit by hand — regenerate with scripts/build-proto.sh after
changes to proto/motion.proto.
"""
EOF
fi

cd "${REPO_ROOT}"

uv run python -m grpc_tools.protoc \
    --proto_path="${PROTO_DIR}" \
    --python_out="${OUT_DIR}" \
    --pyi_out="${OUT_DIR}" \
    --grpc_python_out="${OUT_DIR}" \
    "${PROTO_DIR}/motion.proto"

# grpc_tools emits absolute imports (`import motion_pb2`) which break
# when the stubs live inside a package. Patch the relative import.
sed -i 's/^import motion_pb2/from autoweaver.motion_policy._proto import motion_pb2/' \
    "${OUT_DIR}/motion_pb2_grpc.py"

echo "✓ regenerated stubs in ${OUT_DIR}"
