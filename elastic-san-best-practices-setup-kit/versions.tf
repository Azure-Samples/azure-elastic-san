# Terraform and Provider Version Constraints

terraform {
  required_version = ">= 1.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.85"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.1"
    }
  }

  # Optional: Configure remote state backend
  # Uncomment and configure for production use
  # backend "azurerm" {
  #   resource_group_name  = "tfstate-rg"
  #   storage_account_name = "tfstatestorageaccount"
  #   container_name       = "tfstate"
  #   key                  = "elastic-san.terraform.tfstate"
  # }
}