# Activities Module

## Summary
The Activities module contains Temporal workflow activities for executing automated operations, DOM discovery, self-healing, and preflight planning. These form the building blocks of Quanta's execution workflows.

## File Manifest
* `activities.py`: Core browser execution loops. Inputs: DAG Nodes. Outputs: Execution status and data.
* `discovery_activities.py`: Autonomous exploration tasks. Inputs: URL, Objective. Outputs: Harvested UI elements.
* `healing_activities.py`: Fallback and self-healing logic. Inputs: Failed interactions. Outputs: Recovered state or hard failures.
* `hybrid_activities.py`: Mixed logic for complex interactions. Inputs: Action parameters. Outputs: Result payloads.
* `publish_activities.py`: NATS integration for telemetry. Inputs: Event data. Outputs: Confirmation.
* `recipe_activity.py`: Executes deterministic JSON recipes. Inputs: Preflight recipe. Outputs: Result JSON.
* `sighted_activity.py`: Vision-based planning pipeline tasks. Inputs: Screenshot/DOM. Outputs: Targeted elements.
