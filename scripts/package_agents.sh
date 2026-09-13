#!/usr/bin/env bash
#
# Package the two agent runtimes and upload them to the artifacts bucket.
#
# AgentCore direct code deployment takes a .zip containing an entrypoint .py
# that uses @app.entrypoint (or implements POST /invocations and GET /ping),
# alongside a requirements.txt describing its dependencies. The runtime
# resolves those dependencies when it builds the agent, so the zip stays small
# and we avoid vendoring wheels for an architecture we are not building on.
#
# If a future runtime version stops resolving requirements.txt, set
# VENDOR_DEPS=1 to pip install into the zip instead.
#
# Usage: package_agents.sh <artifacts-bucket> <region>

set -euo pipefail

BUCKET="${1:?artifacts bucket required}"
REGION="${2:-us-east-1}"
VENDOR_DEPS="${VENDOR_DEPS:-0}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build/agents"

rm -rf "${BUILD}"
mkdir -p "${BUILD}"

package_agent() {
  local name="$1"
  local source_dir="${ROOT}/src/agents/${name}"
  local stage="${BUILD}/${name}"
  local zip_path="${BUILD}/${name}.zip"

  echo "==> Packaging ${name}"

  if [[ ! -f "${source_dir}/main.py" ]]; then
    echo "    ERROR: ${source_dir}/main.py not found" >&2
    exit 1
  fi

  mkdir -p "${stage}"
  # Copy the Python sources and the dependency manifest, nothing else.
  find "${source_dir}" -maxdepth 1 -type f \
    \( -name '*.py' -o -name 'requirements.txt' \) \
    -exec cp {} "${stage}/" \;

  if [[ "${VENDOR_DEPS}" == "1" ]]; then
    echo "    Vendoring dependencies into the zip"
    python3 -m pip install \
      --requirement "${source_dir}/requirements.txt" \
      --target "${stage}" \
      --quiet \
      --disable-pip-version-check
  fi

  # -r recurse, -q quiet. Zipping from inside the stage directory keeps
  # main.py at the archive root, which is where the entrypoint must be.
  (cd "${stage}" && zip -rq "${zip_path}" .)

  echo "    $(du -h "${zip_path}" | cut -f1) -> s3://${BUCKET}/agents/${name}.zip"
  aws s3 cp "${zip_path}" "s3://${BUCKET}/agents/${name}.zip" \
    --region "${REGION}" --only-show-errors
}

package_agent orchestrator
package_agent kb_specialist

echo "Agents packaged and uploaded."
