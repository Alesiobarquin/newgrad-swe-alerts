variable "project_name" {
  description = "Short name used as a prefix for Azure resources."
  type        = string
  default     = "newgrad-alerts"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,24}$", var.project_name))
    error_message = "project_name must contain 3-24 lowercase letters, numbers, or hyphens."
  }
}

variable "location" {
  description = "Azure region allowed by this subscription's deployment policy."
  type        = string
  default     = "mexicocentral"
}

variable "vm_size" {
  description = "Azure VM size covered by the Azure for Students introductory allowance."
  type        = string
  default     = "Standard_B2ats_v2"
}

variable "admin_username" {
  description = "Linux administrator account used over SSH."
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key_path" {
  description = "Path to the local public SSH key installed on the VM."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_source_cidr" {
  description = "Only this public IPv4 CIDR may connect to SSH, for example 203.0.113.10/32."
  type        = string

  validation {
    condition     = can(cidrhost(var.ssh_source_cidr, 0)) && endswith(var.ssh_source_cidr, "/32")
    error_message = "ssh_source_cidr must be a single IPv4 address expressed as a /32 CIDR."
  }
}

variable "tags" {
  description = "Tags applied to all supported Azure resources."
  type        = map(string)
  default = {
    application = "newgrad-swe-alerts"
    environment = "production"
    managed-by  = "terraform"
  }
}
