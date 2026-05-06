# Control Plane Controllers Module

## Summary
The Controllers module acts as the HTTP interface for the Go Control Plane. It handles routing, request validation, and delegation to the Temporal or NATS orchestration layers.

## File Manifest
* `execute_controller.go`: Entry point for immediate execution requests. Inputs: Workflow payload. Outputs: Job ID.
* `generator_controller.go`: API for generating automation recipes. Inputs: URL/Objective. Outputs: JSON Recipe.
* `sighted_controller.go`: Interface for the sighted planning pipeline. Inputs: Vision target. Outputs: Plan.
* `workflow_controller.go`: Manages lifecycle of long-running workflows. Inputs: Job ID. Outputs: Workflow status.
