output "public_ip_address" {
  description = "Public IPv4 address of the alert worker VM."
  value       = azurerm_public_ip.app.ip_address
}

output "ssh_command" {
  description = "Command used to connect to the VM after provisioning."
  value       = "ssh ${var.admin_username}@${azurerm_public_ip.app.ip_address}"
}

output "resource_group_name" {
  description = "Resource group containing every resource in this deployment."
  value       = azurerm_resource_group.app.name
}
