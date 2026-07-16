# Output values for the Azure Elastic SAN deployment

output "resource_group_name" {
  description = "Name of the resource group"
  value       = azurerm_resource_group.main.name
}

output "resource_group_location" {
  description = "Location of the resource group"
  value       = azurerm_resource_group.main.location
}

output "virtual_machine_name" {
  description = "Name of the Windows virtual machine"
  value       = azurerm_windows_virtual_machine.main.name
}

output "public_ip_address" {
  description = "Public IP address of the Windows VM"
  value       = azurerm_public_ip.main.ip_address
}

output "private_ip_address" {
  description = "Private IP address of the Windows VM"
  value       = azurerm_network_interface.main.private_ip_address
}

output "admin_username" {
  description = "Administrator username for the VM"
  value       = azurerm_windows_virtual_machine.main.admin_username
}

output "admin_password" {
  description = "Administrator password for the VM"
  value       = random_password.admin_password.result
  sensitive   = true
}

output "elastic_san_name" {
  description = "Name of the Elastic SAN"
  value       = azurerm_elastic_san.main.name
}

output "elastic_san_id" {
  description = "ID of the Elastic SAN"
  value       = azurerm_elastic_san.main.id
}

output "volume_group_name" {
  description = "Name of the Elastic SAN volume group"
  value       = azurerm_elastic_san_volume_group.main.name
}

output "volume_name" {
  description = "Name of the Elastic SAN volume"
  value       = azurerm_elastic_san_volume.main.name
}

output "volume_size_gib" {
  description = "Size of the volume in GiB"
  value       = azurerm_elastic_san_volume.main.size_in_gib
}

output "volume_target_iqn" {
  description = "Target IQN of the Elastic SAN volume"
  value       = azurerm_elastic_san_volume.main.target_iqn
}

output "volume_target_portal_hostname" {
  description = "Target portal hostname of the Elastic SAN volume"
  value       = azurerm_elastic_san_volume.main.target_portal_hostname
}

output "volume_target_portal_port" {
  description = "Target portal port of the Elastic SAN volume"
  value       = azurerm_elastic_san_volume.main.target_portal_port
}

output "rdp_connection_command" {
  description = "Command to connect to the VM via RDP"
  value       = "mstsc /v:${azurerm_public_ip.main.ip_address}"
}

output "next_steps" {
  description = "Instructions for what to do after deployment"
  sensitive   = true
  value = <<-EOT
    Deployment Complete! Next steps:
    
    1. Connect to the VM via RDP:
       mstsc /v:${azurerm_public_ip.main.ip_address}
       Username: ${azurerm_windows_virtual_machine.main.admin_username}
       Password: ${random_password.admin_password.result}
    
    2. The iSCSI MPIO setup script should run automatically during VM boot
    
    3. Verify iSCSI connections with these PowerShell commands:
       Get-Service -Name MSiSCSI
       iscsicli SessionList
       mpclaim -s -d
    
    4. Check if the Elastic SAN volume is mounted and accessible
    
    5. Volume Details:
       - Target IQN: ${azurerm_elastic_san_volume.main.target_iqn}
       - Portal: ${azurerm_elastic_san_volume.main.target_portal_hostname}:${azurerm_elastic_san_volume.main.target_portal_port}
       - Sessions: 32 (configured for optimal performance)
  EOT
}