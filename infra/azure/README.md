# Azure production runbook

This directory contains the Terraform and deployment tooling for the production alert worker. The service runs continuously in Azure; the developer laptop and terminal can be turned off after deployment.

## Deployed architecture

```text
Azure VM (Ubuntu 24.04, Standard_B2ats_v2, Mexico Central)
  └─ Docker daemon (enabled at boot)
      └─ swe-alerts container (--restart unless-stopped)
          └─ Python APScheduler
              ├─ 60-second polls, 08:00–23:00 America/New_York
              ├─ hourly polls, 23:00–08:00 America/New_York
              └─ 08:00 quiet-queue morning burst

External managed services
  ├─ RapidAPI Instagram Downloader: current story media
  ├─ Upstash Redis: claims, deduplication, queues, and shared state
  ├─ Google Gemini: unseen-story image classification
  └─ ntfy: mobile push delivery
```

Azure resources managed by Terraform:

- Resource group `rg-newgrad-alerts`
- `Standard_B2ats_v2` VM with Ubuntu 24.04
- 30 GiB Standard HDD OS disk
- 2 GiB swap configured by cloud-init
- Virtual network, subnet, network interface, and Standard static IPv4 address
- Network security group allowing port 22 only from the configured operator `/32`

The Azure for Students subscription currently enforces an allow-list of Mexico Central, France Central, Germany West Central, Sweden Central, and Denmark East. Mexico Central was selected as the closest eligible region with the required Basv2 quota. A public SKU listing alone is not enough to determine eligibility; the subscription policy is authoritative.

## Cost model

Prices were checked against the Azure Retail Prices API on September 11, 2026:

| Resource | Approximate retail cost |
| :--- | :--- |
| `Standard_B2ats_v2` Linux compute | $0.0103/hour, about $7.52/month; covered by the student offer's 750 monthly hours for the first 12 months |
| Standard static IPv4 | $0.005/hour, about $3.65/month |
| S4 Standard HDD OS disk | About $1.69/month, plus negligible operations |
| Expected first-year credit use | About **$5.34/month** or **$64/year**, before unusual outbound traffic |

Azure pricing and promotional eligibility can change. Confirm actual charges in Azure Cost Management. The static IP and disk continue billing when the VM is merely stopped or deallocated; destroy them with Terraform when the service is no longer needed.

References: [Azure for Students](https://azure.microsoft.com/en-us/free/students/), [IP pricing](https://azure.microsoft.com/en-us/pricing/details/ip-addresses/), and [managed disk pricing](https://azure.microsoft.com/en-us/pricing/details/managed-disks/).

## Prerequisites

- Azure CLI authenticated to the `Azure for Students` subscription
- Terraform 1.8 or newer
- `ssh`, `scp`, `rsync`, and the local Ed25519 public key at `~/.ssh/id_ed25519.pub`
- A complete repository-root `.env`

Verify the active subscription before changing infrastructure:

```bash
az account show --query '{name:name,state:state,isDefault:isDefault}' -o table
```

## Provision or reconcile infrastructure

Run from the repository root:

```bash
export ARM_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
operator_cidr="$(curl -fsS https://api.ipify.org)/32"

terraform -chdir=infra/azure init
terraform -chdir=infra/azure plan \
  -var="ssh_source_cidr=${operator_cidr}"
terraform -chdir=infra/azure apply \
  -var="ssh_source_cidr=${operator_cidr}"
```

Always review the plan before approving an apply. Terraform state is local under this directory and gitignored. Do not delete `terraform.tfstate` or its backup while these resources exist; without state, Terraform cannot safely correlate the configuration with the deployed resources.

The configuration contains no API credentials. Cloud-init installs Docker and Git, enables Docker at boot, creates `/opt/newgrad-swe-alerts`, and configures swap. Runtime secrets are deployed separately.

## Deploy an application update

Run from the repository root:

```bash
./infra/azure/deploy.sh
```

The helper performs this sequence:

1. Reads the VM address from Terraform output.
2. Syncs the current working tree while excluding Git data, Terraform state, `.env`, virtual environments, caches, and logs.
3. Copies `.env` separately and sets mode `600`.
4. Builds the Docker image on the VM.
5. Runs `python -m app.poll_once` as a live Redis/RapidAPI smoke test.
6. Replaces the `swe-alerts` container with log rotation and `--restart unless-stopped`.
7. Confirms the new process and scheduler started.

The update causes a brief container restart. The script deploys the current working tree, including uncommitted changes; commit separately when the changes are ready for version control.

## Routine operations

Set the VM address once per terminal session:

```bash
vm_ip="$(terraform -chdir=infra/azure output -raw public_ip_address)"
```

Check status and recent logs:

```bash
ssh "azureuser@${vm_ip}" \
  'docker inspect swe-alerts --format "status={{.State.Status}} restarts={{.RestartCount}}" && docker logs --tail 100 swe-alerts'
```

Follow logs continuously:

```bash
ssh "azureuser@${vm_ip}" 'docker logs -f swe-alerts'
```

Check resource headroom:

```bash
ssh "azureuser@${vm_ip}" \
  'docker stats --no-stream swe-alerts; free -h; df -h /'
```

Run a live poll manually without disturbing the daemon:

```bash
ssh "azureuser@${vm_ip}" \
  'cd /opt/newgrad-swe-alerts && docker run --rm --env-file .env --init newgrad-swe-alerts:latest python -m app.poll_once'
```

Send two explicit phone-notification probes:

```bash
ssh "azureuser@${vm_ip}" \
  'cd /opt/newgrad-swe-alerts && docker run --rm --env-file .env --init newgrad-swe-alerts:latest python -m app.notify_test'
```

The notification probe sends one emergency alert and one quiet-style alert. It should not be used as a passive health check.

## When the operator public IP changes

SSH is intentionally restricted to the public address used during the last Terraform apply. If SSH times out after changing Wi-Fi networks, traveling, or receiving a new ISP address, update the rule:

```bash
export ARM_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
operator_cidr="$(curl -fsS https://api.ipify.org)/32"
terraform -chdir=infra/azure apply \
  -var="ssh_source_cidr=${operator_cidr}"
```

This changes only the network-security rule when the rest of the infrastructure is unchanged.

## Restart and recovery

Restart only the container:

```bash
ssh "azureuser@${vm_ip}" 'docker restart swe-alerts'
```

Restart the VM:

```bash
az vm restart --resource-group rg-newgrad-alerts --name vm-newgrad-alerts
```

Docker is enabled at boot and the container uses `unless-stopped`, so the alert worker returns automatically after a VM reboot or an unexpected application-process exit. An explicit `docker stop` or `docker kill` is treated as an operator stop; run `docker start swe-alerts` afterward.

## Pause or destroy

Deallocate the VM to stop compute usage temporarily:

```bash
az vm deallocate --resource-group rg-newgrad-alerts --name vm-newgrad-alerts
```

Start it again:

```bash
az vm start --resource-group rg-newgrad-alerts --name vm-newgrad-alerts
```

Deallocation does not remove the disk or static IP charges and no polls run while the VM is offline.

To permanently remove the Azure deployment, preview and then destroy it from the repository root:

```bash
export ARM_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
operator_cidr="$(curl -fsS https://api.ipify.org)/32"
terraform -chdir=infra/azure plan -destroy \
  -var="ssh_source_cidr=${operator_cidr}"
terraform -chdir=infra/azure destroy \
  -var="ssh_source_cidr=${operator_cidr}"
```

Destroy removes the VM, disk, public IP, and networking. The remote `.env` is deleted with the VM disk and is not recoverable unless retained locally.

## Verified deployment baseline

The September 11, 2026 deployment passed all of the following:

- Terraform apply: 8 Azure resources created successfully
- Terraform drift check: no changes
- Azure provisioning and power state: succeeded/running
- cloud-init: completed; Docker active; 2 GiB swap mounted
- live downloader request: 61 current story assets returned
- live Upstash connection and batched deduplication: successful
- real Gemini image classification from the Azure IP: successful
- ntfy emergency and quiet probes: accepted by the service
- multiple one-minute scheduled polls: successful
- unexpected Python-process exit: container restarted automatically
- full Azure VM reboot: container and scheduler returned automatically
- post-reboot and post-deploy polls: successful
- runtime after deployment: about 65 MiB container memory, no recent errors
- local test suite: 64 passing tests
