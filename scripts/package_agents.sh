#!/usr/bin/env bash
#
# Package the two agent runtimes and upload them to the artifacts bucket.
#
# AgentCore direct code deployment takes a .zip with an entrypoint .py at the
# archive root that uses @app.entrypoint (or serves POST /invocations and
# GET /ping). It does NOT install requirements.txt for you — a zip containing
# only source fails at startup with:
#
#   ModuleNotFoundError: No module named 'bedrock_agentcore'
#
# and, because a crashing container never finishes booting, the invocation
# surfaces as a confusing "Runtime initialization time exceeded" rather than an
# import error. So dependencies are vendored into the zip.
#
# Two details that cost an afternoon to find:
#
#   * The runtimes are Linux aarch64. Wheels must be resolved for that platform
#     from whatever machine is building, hence the explicit --platform and
#     --only-binary flags rather than a plain pip install.
#
#   * Do NOT delete *.dist-info to save space. Several libraries read their own
#     version at import time via importlib.metadata, and without the metadata
#     directory they raise PackageNotFoundError. Only __pycache__ and console
#     scripts are safe to strip.
#
# Usage: package_agents.sh <artifacts-bucket> [region]

set -euo pipefail

BUCKET="${1:?artifacts bucket required}"
REGION="${2:-us-east-1}"

# The runtime platform to resolve wheels for. Override only if AWS changes the
# architecture AgentCore runs on.
PLATFORM="${PLATFORM:-manylinux2014_aarch64}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

# Set SKIP_VENDOR=1 to ship source only. Useful when iterating on agent code
# with unchanged dependencies, since it turns a 28 MB upload into a 20 KB one.
SKIP_VENDOR="${SKIP_VENDOR:-0}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build/agents"
VENDOR="${ROOT}/build/vendor"

# Prefer the project venv's pip so the build does not depend on whatever
# happens to be on PATH.
PIP="${ROOT}/.venv/bin/pip"
[[ -x "${PIP}" ]] || PIP="python3 -m pip"

mkdir -p "${BUILD}"

package_agent() {
  local name="$1"
  local source_dir="${ROOT}/src/agents/${name}"
  local stage="${VENDOR}/${name}"
  local zip_path="${BUILD}/${name}.zip"

  echo "==> Packaging ${name}"

  if [[ ! -f "${source_dir}/main.py" ]]; then
    echo "    ERROR: ${source_dir}/main.py not found" >&2
    exit 1
  fi

  if [[ "${SKIP_VENDOR}" == "1" && -d "${stage}" ]]; then
    echo "    reusing vendored dependencies (SKIP_VENDOR=1)"
  else
    rm -rf "${stage}"
    mkdir -p "${stage}"
    echo "    resolving dependencies for ${PLATFORM} / py${PYTHON_VERSION}"
    ${PIP} install \
      --requirement "${source_dir}/requirements.txt" \
      --target "${stage}" \
      --platform "${PLATFORM}" \
      --python-version "${PYTHON_VERSION}" \
      --only-binary=:all: \
      --quiet --disable-pip-version-check

    # Safe to remove: compiled caches for the wrong interpreter, and console
    # entry points nothing invokes. *.dist-info stays — see the header.
    find "${stage}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf "${stage}/bin"
  fi

  # Source last, so it overwrites anything a dependency happens to shadow.
  cp "${source_dir}"/*.py "${stage}/"
  cp "${source_dir}/requirements.txt" "${stage}/"

  rm -f "${zip_path}"
  # Zipped from inside the stage directory so main.py sits at the archive root,
  # which is where the entry point must be.
  (cd "${stage}" && zip -rq "${zip_path}" .)

  echo "    $(du -h "${zip_path}" | cut -f1) -> s3://${BUCKET}/agents/${name}.zip"
  aws s3 cp "${zip_path}" "s3://${BUCKET}/agents/${name}.zip" \
    --region "${REGION}" --only-show-errors
}

package_agent orchestrator
package_agent kb_specialist

echo
echo "Agents packaged and uploaded."
echo "A runtime does not re-read its zip automatically — run 'make deploy', or"
echo "update the runtime, to roll a new version that picks this up."
