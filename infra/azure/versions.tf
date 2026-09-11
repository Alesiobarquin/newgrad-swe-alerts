terraform {
  required_version = ">= 1.8.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}

  # This worker only uses providers registered explicitly during setup.
  # Avoid waiting for AzureRM to register unrelated services such as AVS.
  resource_provider_registrations = "none"
}
