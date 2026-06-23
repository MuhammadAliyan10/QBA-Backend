import os

RENAME_MAP = {
    "coreWorkflow.py": "core_workflow.py",
    "discoveryActivities.py": "discovery_activities.py",
    "healingActivities.py": "healing_activities.py",
    "hybridActivities.py": "hybrid_activities.py",
    "recipeActivity.py": "recipe_activity.py",
    "publishActivities.py": "publish_activities.py",
    "sightedActivity.py": "sighted_activity.py",
    "executeUniversalAgent.py": "execute_universal_agent.py",
    "apiRoutes.py": "api_routes.py",
    "verifySanitizer.py": "verify_sanitizer.py",
    "domHarvester.py": "dom_harvester.py",
    "smartFinder.py": "smart_finder.py",
    "safeClient.py": "safe_client.py",
    "userFacingLogger.py": "user_facing_logger.py",
    "networkSniffer.py": "network_sniffer.py",
    "nervousSystem.py": "nervous_system.py",
    "recipeValidator.py": "recipe_validator.py",
    "recipeEngine.py": "recipe_engine.py",
    "recipeManager.py": "recipe_manager.py",
    "operatorRealizer.py": "operator_realizer.py",
    "recipeConverter.py": "recipe_converter.py",
    "recipeSchema.py": "recipe_schema.py",
    "elementMatcher.py": "element_matcher.py",
    "sightedPlanner.py": "sighted_planner.py",
    "intentParser.py": "intent_parser.py",
    "siteAtlas.py": "site_atlas.py",
    "nodeBuilder.py": "node_builder.py",
    "goalExecutor.py": "goal_executor.py",
    "tokenTelemetry.py": "token_telemetry.py",
    "sightedPipeline.py": "sighted_pipeline.py",
    "mathUtils.py": "math_utils.py",
    "accountManager.py": "account_manager.py",
    "stateSignature.py": "state_signature.py",
    "actionMap.py": "action_map.py",
    "urlUtils.py": "url_utils.py",
    "ragService.py": "rag_service.py",
    "staticValidator.py": "static_validator.py",
    "piiScrubber.py": "pii_scrubber.py",
    "browserWorkflow.py": "browser_workflow.py",
    "generationWorkflow.py": "generation_workflow.py",
    "baseAction.py": "base_action.py",
    "extractAction.py": "extract_action.py",
    "loginAction.py": "login_action.py",
    "clickAction.py": "click_action.py",
    "browserStreamer.py": "browserStreamer.py",
    "inputBridge.py": "inputBridge.py",
    "goGateway.py": "goGateway.py",
}

root_dir = "apps/execution-plane/src"
for dirpath, dirnames, filenames in os.walk(root_dir):
    for fname in filenames:
        if fname in RENAME_MAP:
            old = os.path.join(dirpath, fname)
            new = os.path.join(dirpath, RENAME_MAP[fname])
            if old != new:
                print(f"Reverting: {old} -> {new}")
                os.rename(old, new)
