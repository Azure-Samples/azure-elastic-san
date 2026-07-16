# Variables for Azure Elastic SAN and Windows VM deployment

variable "prefix" {
  description = "A prefix used for all resources"
  type        = string
  default     = "esan"
}

variable "location" {
  description = "The Azure Region in which all resources should be created"
  type        = string
  default     = "East US"
}

variable "resource_group_name" {
  description = "The name of the resource group"
  type        = string
  default     = "rg-elastic-san-demo"
}

variable "vm_size" {
  description = "Size of the Virtual Machine (Gen 5+ recommended for Elastic SAN)"
  type        = string
  default     = "Standard_D48as_v7"
  
  validation {
    condition = contains([
      "Standard_D2s_v3", "Standard_D4s_v3", "Standard_D8s_v3", "Standard_D16s_v3", "Standard_D32s_v3",
      "Standard_E2s_v3", "Standard_E4s_v3", "Standard_E8s_v3", "Standard_E16s_v3", "Standard_E32s_v3",
      "Standard_F2s_v2", "Standard_F4s_v2", "Standard_F8s_v2", "Standard_F16s_v2", "Standard_F32s_v2",
      "Standard_M8ms", "Standard_M16ms", "Standard_M32ms", "Standard_M64ms",
      "Standard_D2as_v7", "Standard_D4as_v7", "Standard_D8as_v7", "Standard_D16as_v7", "Standard_D32as_v7", "Standard_D48as_v7", "Standard_D64as_v7", "Standard_D96as_v7",
      "Standard_E2as_v7", "Standard_E4as_v7", "Standard_E8as_v7", "Standard_E16as_v7", "Standard_E32as_v7", "Standard_E48as_v7", "Standard_E64as_v7", "Standard_E96as_v7"
    ], var.vm_size)
    error_message = "The VM size must be a supported Azure VM size optimized for Elastic SAN workloads (D/E/F/M series v3+ or v7+ series)."
  }
}

variable "admin_username" {
  description = "The username for the local administrator account"
  type        = string
  default     = "esanadmin"
}

variable "iscsi_sessions" {
  description = "Number of iSCSI sessions to establish for MPIO (recommended: 32)"
  type        = number
  default     = 32
  
  validation {
    condition     = var.iscsi_sessions >= 1 && var.iscsi_sessions <= 32
    error_message = "Number of iSCSI sessions must be between 1 and 32."
  }
}

variable "enable_accelerated_networking" {
  description = "Enable accelerated networking for optimal Elastic SAN performance"
  type        = bool
  default     = true
}

variable "tags" {
  description = "A map of tags to add to all resources"
  type        = map(string)
  default = {
    Environment = "Demo"
    Project     = "ElasticSAN-MPIO"
    Owner       = "Infrastructure Team"
    BestPractices = "Azure-Optimized"
  }
}

variable "elastic_san_base_size_tib" {
  description = "Base size of the Elastic SAN in TiB"
  type        = number
  default     = 20
  
  validation {
    condition     = var.elastic_san_base_size_tib >= 1 && var.elastic_san_base_size_tib <= 200
    error_message = "Elastic SAN base size must be between 1 and 200 TiB."
  }
}

variable "volume_size_gib" {
  description = "Size of the Elastic SAN volume in GiB"
  type        = number
  default     = 1024  # 1 TiB
  
  validation {
    condition     = var.volume_size_gib >= 1 && var.volume_size_gib <= 65536
    error_message = "Volume size must be between 1 GiB and 64 TiB (65536 GiB)."
  }
}