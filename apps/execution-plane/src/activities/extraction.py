import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("activity")

async def perform_extraction(
    page, finder, step_params, params, job_id, node_id, global_sniffer, NervousSystem, user_logger
):
    action = "EXTRACT"
    intent = step_params["intent"]
    attr = step_params.get("attribute")

    raw_value = None
    is_network_extraction = False

    # PHASE 3 FIX: Network First Intent-Key Matching
    if global_sniffer and hasattr(global_sniffer, 'captured_responses'):
        # TASK 7 FIX: Purge guest-state network payloads captured during navigation.
        # This prevents scoring public trending repos from the login landing page.
        global_sniffer.captured_responses.clear()
        
    # Re-check for new responses captured after hydration
    if global_sniffer and hasattr(global_sniffer, 'captured_responses') and global_sniffer.captured_responses:
        best_payload = None
        best_score = -1
        # Basic tokenization of user intent ("price of iphone" -> ["price", "iphone"])
        intent_keywords = [k.lower() for k in intent.split() if len(k) > 2]

        for resp in global_sniffer.captured_responses:
            data_str = str(resp["data"]).lower()
            # Score based on how many intent keywords exist in the raw JSON payload
            score = sum(3 for k in intent_keywords if k in data_str)
            # Tie-breaker: larger payloads are typically more data rich
            score += (resp["size"] / 10000.0)

            if score > best_score:
                best_score = score
                best_payload = resp["data"]

        if best_payload is not None and best_score > 0:
            raw_value = best_payload
            is_network_extraction = True
            global_sniffer.captured_responses = [] # Prevent stale reads
            logger.info(f"[{job_id}] Extracted '{intent}' via JSON API Interception (bypassing DOM). Score: {best_score:.2f}")

    if not is_network_extraction:
        # PHASE 3: Algorithmic DOM Parsing Fallback
        result = await finder.find(intent, timeout=10000, scan_mode="all")
        if not result.found:
            raise Exception(f"Element not found: {intent}")
        element = result.element

        if attr:
            raw_value = await element.get_attribute(attr)
        else:
            # Inspect tag for table extraction
            tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "table":
                raw_value = await element.evaluate("""el => {
                    const headers = Array.from(el.querySelectorAll('th')).map((th, i) => th.innerText.trim() || `col_${i+1}`);
                    const rows = Array.from(el.querySelectorAll('tbody tr, tr')).filter(tr => !tr.querySelector('th'));

                    let keys = headers;
                    if (keys.length === 0 && rows.length > 0) {
                        const maxCols = Math.max(...rows.map(tr => (tr.cells ? tr.cells.length : 0)));
                        keys = Array.from({length: maxCols}, (_, i) => `col_${i+1}`);
                    }

                    const result = [];
                    for (const row of rows) {
                        if (!row.cells) continue;
                        const cells = Array.from(row.cells).map(td => td.innerText.trim());
                        if (cells.length > 0 && cells.some(c => c !== '')) {
                            const rowDict = {};
                            for (let i = 0; i < keys.length; i++) {
                                rowDict[keys[i]] = cells[i] !== undefined ? cells[i] : null;
                            }
                            result.push(rowDict);
                        }
                    }
                    return result;
                }""")
            else:
                raw_value = await element.text_content()

    # TYPE INFERENCE
    typed_type = "string"
    typed_content = raw_value

    if isinstance(raw_value, list):
        typed_type = "table"
    elif isinstance(raw_value, str):
        val_str = raw_value.strip()
        lower_str = val_str.lower()
        if lower_str == "true":
            typed_type = "boolean"
            typed_content = True
        elif lower_str == "false":
            typed_type = "boolean"
            typed_content = False
        else:
            import re
            # Basic heuristic for full-string numbers (allow formatted curr/commas)
            if re.match(r'^[-+]?[^\d.-]*[\d.,]+[^\d.-]*$', val_str):
                clean_str = re.sub(r'[^\d.-]', '', val_str)
                if clean_str and clean_str != "-" and clean_str != ".":
                    try:
                        if '.' in clean_str:
                            typed_content = float(clean_str)
                            typed_type = "number"
                        else:
                            typed_content = int(clean_str)
                            typed_type = "number"
                    except ValueError:
                        pass

    # PHASE 3.5: Apply Strict Pydantic Guardrails
    from .validator import ExtractionValidator
    from pydantic import ValidationError

    try:
        validator_instance = ExtractionValidator(intent=intent, value=typed_content)
        typed_content = validator_instance.value
    except (ValueError, ValidationError) as e:
        logger.warning(f"[{job_id}] Hallucination intercepted by Validator: {e}")
        typed_content = None
        typed_type = "null"

    payload_dict = {
        "type": typed_type,
        "content": typed_content,
        "confidence": 1.0
    }
    data_json = json.dumps(payload_dict)

    # TELEMETRY: Extraction Payload
    await NervousSystem.publish(
        f"quanta.telemetry.{job_id}",
        json.dumps({"type": "log", "message": f"[Extractor] Payload: {data_json}"})
    )

    logger.info(f"[{job_id}] Extracted '{intent}': ({typed_type}) {str(typed_content)[:100]}")
    await user_logger.info("FOUND_ELEMENT", element=f"Extracted data from {intent}")

    publish_str = str(typed_content) if typed_type != "table" else f"Table ({len(typed_content)} rows)"
    await NervousSystem.publish_update(
        job_id, "RUNNING", f"Extracted: {publish_str[:30]}...", node_id, data=data_json
    )
