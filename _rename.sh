#!/bin/bash
set -e

cd "$(dirname "$0")"
EP="apps/execution-plane/src"

# ═══════════════════════════════════════════════════════════════
# STEP 1: RENAME FILES (plain mv — handles tracked + untracked)
# ═══════════════════════════════════════════════════════════════

mv "$EP/activities/discoveryActivities.py"  "$EP/activities/discovery_activities.py"
mv "$EP/activities/healingActivities.py"    "$EP/activities/healing_activities.py"
mv "$EP/activities/hybridActivities.py"     "$EP/activities/hybrid_activities.py"
mv "$EP/activities/publishActivities.py"    "$EP/activities/publish_activities.py"
mv "$EP/activities/recipeActivity.py"       "$EP/activities/recipe_activity.py"
mv "$EP/activities/sightedActivity.py"      "$EP/activities/sighted_activity.py"

mv "$EP/apiRoutes.py"                       "$EP/api_routes.py"

mv "$EP/core/AccountManager.py"             "$EP/core/account_manager.py"
mv "$EP/core/NervousSystem.py"              "$EP/core/nervous_system.py"
mv "$EP/core/NetworkSniffer.py"             "$EP/core/network_sniffer.py"
mv "$EP/core/UserFacingLogger.py"           "$EP/core/user_facing_logger.py"

mv "$EP/core/browser/actionMap.py"          "$EP/core/browser/action_map.py"
mv "$EP/core/browser/domHarvester.py"       "$EP/core/browser/dom_harvester.py"
mv "$EP/core/browser/stateSignature.py"     "$EP/core/browser/state_signature.py"

mv "$EP/core/llm/safeClient.py"             "$EP/core/llm/safe_client.py"

mv "$EP/core/planning/elementMatcher.py"    "$EP/core/planning/element_matcher.py"
mv "$EP/core/planning/goalExecutor.py"      "$EP/core/planning/goal_executor.py"
mv "$EP/core/planning/intentParser.py"      "$EP/core/planning/intent_parser.py"
mv "$EP/core/planning/nodeBuilder.py"       "$EP/core/planning/node_builder.py"
mv "$EP/core/planning/sightedPipeline.py"   "$EP/core/planning/sighted_pipeline.py"
mv "$EP/core/planning/sightedPlanner.py"    "$EP/core/planning/sighted_planner.py"
mv "$EP/core/planning/siteAtlas.py"         "$EP/core/planning/site_atlas.py"
mv "$EP/core/planning/tokenTelemetry.py"    "$EP/core/planning/token_telemetry.py"

mv "$EP/core/rag/ragService.py"             "$EP/core/rag/rag_service.py"
mv "$EP/core/rag/staticValidator.py"        "$EP/core/rag/static_validator.py"

mv "$EP/core/recipe/operatorRealizer.py"    "$EP/core/recipe/operator_realizer.py"
mv "$EP/core/recipe/recipeEngine.py"        "$EP/core/recipe/recipe_engine.py"
mv "$EP/core/recipe/recipeManager.py"       "$EP/core/recipe/recipe_manager.py"
mv "$EP/core/recipe/recipeSchema.py"        "$EP/core/recipe/recipe_schema.py"
mv "$EP/core/recipe/recipeValidator.py"     "$EP/core/recipe/recipe_validator.py"

mv "$EP/core/selector/smartFinder.py"       "$EP/core/selector/smart_finder.py"
mv "$EP/core/selector/utils/mathUtils.py"   "$EP/core/selector/utils/math_utils.py"

mv "$EP/workflows/browserWorkflow.py"       "$EP/workflows/browser_workflow.py"
mv "$EP/workflows/generationWorkflow.py"    "$EP/workflows/generation_workflow.py"

echo "[Phase 1a] All files renamed."

# ═══════════════════════════════════════════════════════════════
# STEP 2: REWRITE ALL IMPORTS
# ═══════════════════════════════════════════════════════════════

PYFILES=$(find "$EP" -name "*.py" -not -path "*__pycache__*" -not -path "*venv*" -not -path "*node_modules*")

for f in $PYFILES; do
  sed -i '' \
    -e 's/activities\.discoveryActivities/activities.discovery_activities/g' \
    -e 's/activities\.healingActivities/activities.healing_activities/g' \
    -e 's/activities\.hybridActivities/activities.hybrid_activities/g' \
    -e 's/activities\.publishActivities/activities.publish_activities/g' \
    -e 's/activities\.recipeActivity/activities.recipe_activity/g' \
    -e 's/activities\.sightedActivity/activities.sighted_activity/g' \
    -e 's/from apiRoutes/from api_routes/g' \
    -e 's/import apiRoutes/import api_routes/g' \
    -e 's/core\.AccountManager/core.account_manager/g' \
    -e 's/core\.NervousSystem/core.nervous_system/g' \
    -e 's/core\.NetworkSniffer/core.network_sniffer/g' \
    -e 's/core\.UserFacingLogger/core.user_facing_logger/g' \
    -e 's/core\.browser\.actionMap/core.browser.action_map/g' \
    -e 's/core\.browser\.domHarvester/core.browser.dom_harvester/g' \
    -e 's/core\.browser\.stateSignature/core.browser.state_signature/g' \
    -e 's/core\.llm\.safeClient/core.llm.safe_client/g' \
    -e 's/core\.planning\.elementMatcher/core.planning.element_matcher/g' \
    -e 's/core\.planning\.goalExecutor/core.planning.goal_executor/g' \
    -e 's/core\.planning\.intentParser/core.planning.intent_parser/g' \
    -e 's/core\.planning\.nodeBuilder/core.planning.node_builder/g' \
    -e 's/core\.planning\.sightedPipeline/core.planning.sighted_pipeline/g' \
    -e 's/core\.planning\.sightedPlanner/core.planning.sighted_planner/g' \
    -e 's/core\.planning\.siteAtlas/core.planning.site_atlas/g' \
    -e 's/core\.planning\.tokenTelemetry/core.planning.token_telemetry/g' \
    -e 's/core\.rag\.ragService/core.rag.rag_service/g' \
    -e 's/core\.rag\.staticValidator/core.rag.static_validator/g' \
    -e 's/core\.recipe\.operatorRealizer/core.recipe.operator_realizer/g' \
    -e 's/core\.recipe\.recipeEngine/core.recipe.recipe_engine/g' \
    -e 's/core\.recipe\.recipeManager/core.recipe.recipe_manager/g' \
    -e 's/core\.recipe\.recipeSchema/core.recipe.recipe_schema/g' \
    -e 's/core\.recipe\.recipeValidator/core.recipe.recipe_validator/g' \
    -e 's/core\.selector\.smartFinder/core.selector.smart_finder/g' \
    -e 's/core\.selector\.utils\.mathUtils/core.selector.utils.math_utils/g' \
    -e 's/workflows\.browserWorkflow/workflows.browser_workflow/g' \
    -e 's/workflows\.generationWorkflow/workflows.generation_workflow/g' \
    "$f"
done

echo "[Phase 1b] All imports rewritten."
echo "[Phase 1] Complete."
