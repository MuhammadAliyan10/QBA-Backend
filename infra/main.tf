# =============================================================================
# QUANTA - HYPER-CONVERGED SERVERLESS INFRASTRUCTURE
# Cost-Optimized for Azure Student / Free Tier
# =============================================================================

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "quanta"
}

variable "environment" {
  description = "Environment (prod/dev)"
  type        = string
  default     = "prod"
}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
  subscription_id = var.subscription_id
}

resource "random_id" "unique" {
  byte_length = 4
}

locals {
  acr_name            = "${var.project_name}${var.environment}acr${random_id.unique.hex}"
  resource_group_name = "${var.project_name}-${var.environment}-rg"
  env_name            = "${var.project_name}-${var.environment}-env"
}

# 1. THE "FREE" FOUNDATION
resource "azurerm_resource_group" "main" {
  name     = local.resource_group_name
  location = var.location
}

resource "azurerm_container_registry" "main" {
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = true
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.project_name}-${var.environment}-logs"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "main" {
  name                       = local.env_name
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
}

# 2. THE "ORGANS" (NATS & REDIS)
resource "azurerm_container_app" "nats" {
  name                         = "nats"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  template {
    container {
      name   = "nats"
      image  = "nats:alpine"
      cpu    = 0.25
      memory = "0.5Gi"
    }
    min_replicas = 1
    max_replicas = 1
  }

  ingress {
    external_enabled = false
    target_port      = 4222
    exposed_port     = 4222
    transport        = "tcp"
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }
}

resource "azurerm_container_app" "redis" {
  name                         = "redis"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  template {
    container {
      name   = "redis"
      image  = "redis:alpine"
      cpu    = 0.25
      memory = "0.5Gi"
    }
    min_replicas = 1
    max_replicas = 1
  }

  ingress {
    external_enabled = false
    target_port      = 6379
    exposed_port     = 6379
    transport        = "tcp"
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }
}

# 3. OUTPUTS (Dynamic & Correct)
output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "acr_username" {
  value     = azurerm_container_registry.main.admin_username
  sensitive = true
}

output "acr_password" {
  value     = azurerm_container_registry.main.admin_password
  sensitive = true
}

output "nats_url" {
  description = "Internal NATS Connection String"
  value       = "nats://${azurerm_container_app.nats.ingress[0].fqdn}:4222"
}

output "redis_url" {
  description = "Internal Redis Connection String"
  value       = "redis://${azurerm_container_app.redis.ingress[0].fqdn}:6379/0"
}
