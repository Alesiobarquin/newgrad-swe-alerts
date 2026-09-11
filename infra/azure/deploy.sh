#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "${script_dir}/../.." && pwd)"
remote_dir="/opt/newgrad-swe-alerts"
remote_user="azureuser"
container_name="swe-alerts"
image_name="newgrad-swe-alerts:latest"

if [[ ! -f "${repo_dir}/.env" ]]; then
  echo "Missing ${repo_dir}/.env" >&2
  exit 1
fi

vm_ip="$(terraform -chdir="${script_dir}" output -raw public_ip_address)"
remote="${remote_user}@${vm_ip}"

echo "Syncing application files to ${remote_dir}..."
rsync -az \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.env' \
  --exclude='infra/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='tmp/' \
  --exclude='*.log' \
  -e 'ssh -o BatchMode=yes' \
  "${repo_dir}/" "${remote}:${remote_dir}/"

echo "Copying runtime environment separately..."
scp -q -o BatchMode=yes "${repo_dir}/.env" "${remote}:${remote_dir}/.env"
ssh -o BatchMode=yes "${remote}" "chmod 0600 ${remote_dir}/.env"

echo "Building and smoke-testing the image..."
ssh -o BatchMode=yes "${remote}" \
  "cd ${remote_dir} && docker build -t ${image_name} . && docker run --rm --env-file .env --init ${image_name} python -m app.poll_once"

echo "Replacing the long-running worker..."
ssh -o BatchMode=yes "${remote}" "
  cd ${remote_dir}
  if docker container inspect ${container_name} >/dev/null 2>&1; then
    docker rm -f ${container_name} >/dev/null
  fi
  docker run -d \\
    --name ${container_name} \\
    --restart unless-stopped \\
    --env-file .env \\
    --init \\
    --log-opt max-size=10m \\
    --log-opt max-file=3 \\
    ${image_name} >/dev/null
  sleep 3
  docker inspect ${container_name} --format 'status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}} image={{.Config.Image}}'
  docker logs --tail 20 ${container_name}
"

echo "Deployment complete."
