# Azure Elastic SAN with Windows VM and iSCSI MPIO Configuration
# This Terraform configuration deploys a 20 TiB Elastic SAN in East US zone 1 
# with a Windows VM configured for optimal iSCSI MPIO connectivity

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}

# Random password for VM admin
resource "random_password" "admin_password" {
  length  = 16
  special = true
  upper   = true
  lower   = true
  numeric = true
}

# Resource Group
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location

  tags = var.tags
}

# Virtual Network with Service Endpoints
resource "azurerm_virtual_network" "main" {
  name                = "${var.prefix}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  tags = var.tags
}

# Subnet with Storage Service Endpoint
resource "azurerm_subnet" "internal" {
  name                 = "internal"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.2.0/24"]

  # Enable service endpoint for Storage to connect to Elastic SAN
  service_endpoints = ["Microsoft.Storage.Global"]
}

# Network Security Group and rules
resource "azurerm_network_security_group" "main" {
  name                = "${var.prefix}-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  # Allow RDP
  security_rule {
    name                       = "RDP"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3389"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # Allow iSCSI traffic
  security_rule {
    name                       = "iSCSI"
    priority                   = 1002
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3260"
    source_address_prefix      = "10.0.0.0/16"
    destination_address_prefix = "*"
  }

  tags = var.tags
}

# Associate Network Security Group to Subnet
resource "azurerm_subnet_network_security_group_association" "main" {
  subnet_id                 = azurerm_subnet.internal.id
  network_security_group_id = azurerm_network_security_group.main.id
}

# Public IP for VM
resource "azurerm_public_ip" "main" {
  name                = "${var.prefix}-pip"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1"]

  tags = var.tags
}

# Network Interface with Accelerated Networking
resource "azurerm_network_interface" "main" {
  name                          = "${var.prefix}-nic"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  accelerated_networking_enabled = var.enable_accelerated_networking  # Best practice for Elastic SAN performance

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.internal.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.main.id
  }

  tags = var.tags
}

# Associate Network Security Group to Network Interface
resource "azurerm_network_interface_security_group_association" "main" {
  network_interface_id      = azurerm_network_interface.main.id
  network_security_group_id = azurerm_network_security_group.main.id
}

# Elastic SAN
resource "azurerm_elastic_san" "main" {
  name                = "${var.prefix}-esan"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  
  # Base unit is 1 TiB, so 20 base units = 20 TiB
  base_size_in_tib = var.elastic_san_base_size_tib
  
  sku {
    name = "Premium_LRS"
    tier = "Premium"
  }

  zones = ["1"]

  tags = var.tags
}

# Volume Group
resource "azurerm_elastic_san_volume_group" "main" {
  name            = "${var.prefix}-vg"
  elastic_san_id  = azurerm_elastic_san.main.id
  encryption_type = "EncryptionAtRestWithPlatformKey"
  protocol_type   = "Iscsi"

  # Configure service endpoint access
  network_rule {
    subnet_id = azurerm_subnet.internal.id
    action    = "Allow"
  }
}

# Elastic SAN Volume (1 TiB)
resource "azurerm_elastic_san_volume" "main" {
  name            = "${var.prefix}-volume"
  volume_group_id = azurerm_elastic_san_volume_group.main.id
  size_in_gib     = var.volume_size_gib
}

# Data source to get the latest Windows Server image
data "azurerm_platform_image" "main" {
  location  = azurerm_resource_group.main.location
  publisher = "MicrosoftWindowsServer"
  offer     = "WindowsServer"
  sku       = "2022-datacenter-azure-edition"
}

# Windows Virtual Machine in Zone 1
resource "azurerm_windows_virtual_machine" "main" {
  name                = "${var.prefix}-vm"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  size                = var.vm_size
  admin_username      = var.admin_username
  admin_password      = random_password.admin_password.result
  zone                = "1"

  network_interface_ids = [
    azurerm_network_interface.main.id,
  ]

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }

  source_image_reference {
    publisher = "MicrosoftWindowsServer"
    offer     = "WindowsServer"
    sku       = "2022-datacenter-azure-edition"
    version   = "latest"
  }

  # Enable managed identity for Azure auth
  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

# Role assignment for VM managed identity to access Elastic SAN
resource "azurerm_role_assignment" "vm_elastic_san" {
  scope                = azurerm_elastic_san.main.id
  role_definition_name = "Reader"
  principal_id         = azurerm_windows_virtual_machine.main.identity[0].principal_id
}

resource "azurerm_role_assignment" "vm_resource_group" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Reader"
  principal_id         = azurerm_windows_virtual_machine.main.identity[0].principal_id
}

# Custom script extension for iSCSI setup (replaces unavailable ElasticSanExtension)
resource "azurerm_virtual_machine_extension" "iscsi_setup" {
  name                 = "iSCSI-Setup"
  virtual_machine_id   = azurerm_windows_virtual_machine.main.id
  publisher            = "Microsoft.Compute"
  type                 = "CustomScriptExtension"
  type_handler_version = "1.10"

  settings = jsonencode({
    commandToExecute = "powershell -ExecutionPolicy Bypass -Command \"$ErrorActionPreference='Stop'; Set-Service -Name 'MSiSCSI' -StartupType Automatic; Start-Service -Name 'MSiSCSI'; if ((Get-WindowsFeature -Name 'Multipath-IO').InstallState -ne 'Installed') { Install-WindowsFeature -Name 'Multipath-IO' -IncludeManagementTools | Out-Null; }; Enable-MSDSMAutomaticClaim -BusType iSCSI -ErrorAction SilentlyContinue | Out-Null; Set-PSRepository -Name 'PSGallery' -InstallationPolicy Trusted -ErrorAction SilentlyContinue; Install-PackageProvider -Name 'NuGet' -Force -Scope AllUsers | Out-Null; if ((Get-Module -ListAvailable -Name 'Az.Accounts' -ErrorAction SilentlyContinue) -eq $null) { Install-Module -Name 'Az.Accounts' -Force -Scope AllUsers -AllowClobber; }; if ((Get-Module -ListAvailable -Name 'Az.ElasticSan' -ErrorAction SilentlyContinue) -eq $null) { Install-Module -Name 'Az.ElasticSan' -Force -Scope AllUsers -AllowClobber; }; Import-Module Az.Accounts; Import-Module Az.ElasticSan; Connect-AzAccount -Identity | Out-Null; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $scriptPath = Join-Path $env:TEMP 'elastic-san-connect.ps1'; (New-Object System.Net.WebClient).DownloadFile('https://raw.githubusercontent.com/Azure-Samples/azure-elastic-san/main/PSH%20(Windows)%20Multi-Session%20Connect%20Scripts/ElasticSanDocScripts0523/connect.ps1', $scriptPath); & $scriptPath -ResourceGroupName '${azurerm_resource_group.main.name}' -ElasticSanName '${azurerm_elastic_san.main.name}' -VolumeGroupName '${azurerm_elastic_san_volume_group.main.name}' -VolumeName '${azurerm_elastic_san_volume.main.name}' -NumSession ${var.iscsi_sessions}\""
  })

  depends_on = [
    azurerm_elastic_san_volume.main,
    azurerm_role_assignment.vm_elastic_san,
    azurerm_role_assignment.vm_resource_group
  ]
}

# Configuration for Elastic SAN connection
locals {
  elastic_san_config = {
    volume_name = azurerm_elastic_san_volume.main.name
    session_count = 32  # Maximum sessions for optimal performance
  }
}