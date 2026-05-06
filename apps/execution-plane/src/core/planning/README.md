# Core Planning Module

## Summary
The Planning module is the intelligence layer of the Execution Plane. It parses natural language intents, harvests DOM context, and utilizes LLMs to generate deterministic execution DAGs.

## File Manifest
* `element_matcher.py`: Uses heuristics to align intent with UI elements. Inputs: Intent, DOM. Outputs: Target element.
* `goal_executor.py`: Translates high-level goals into sequential actions. Inputs: Goal JSON. Outputs: Playwright operations.
* `harvester.py`: Extracts and prunes the DOM. Inputs: Browser Context. Outputs: Token-optimized DOM tree.
* `intent_parser.py`: Breaks down user prompts. Inputs: String prompt. Outputs: Structured intent schema.
* `node_builder.py`: Constructs the execution DAG. Inputs: Steps. Outputs: Node graph.
* `sighted_pipeline.py`: Orchestrator for the "Harvest-First" architecture. Inputs: Objective. Outputs: Pipeline state.
* `sighted_planner.py`: Integrates LLM planning with sighted context. Inputs: Screen/DOM data. Outputs: Action plan.
* `site_atlas.py`: Maps and tracks visited domains. Inputs: URLs. Outputs: Domain topology graph.
* `token_telemetry.py`: Monitors and limits LLM token usage. Inputs: Prompts/Responses. Outputs: Usage metrics.
