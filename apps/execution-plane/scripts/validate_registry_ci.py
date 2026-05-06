import os
import json
import sys
from typing import Dict, Any

def validate_registry_ci(path=".quanta_registry.json"):
    print("Starting CI Registry Validation")
    if not os.path.exists(path):
        print("PASS: No registry exists yet.")
        sys.exit(0)

    with open(path, "r") as f:
        try:
            data = json.load(f)
        except Exception:
            print("FAIL: Invalid JSON parsing")
            sys.exit(1)

    errors = []
    invalid_samples = []
    total_records = 0
    passed = 0

    counters = {
        "key_recompute_mismatch_count": 0,
        "arg_schema_hash_mismatch_count": 0,
        "invalid_hash_length_count": 0,
        "invalid_hash_charset_count": 0,
        "non_rfc3339_timestamp_count": 0,
        "invalid_last_outcome_count": 0,
        "xpath_rank1_violation_count": 0,
        "xpath_only_without_flag_count": 0,
        "xpath_priority_order_violation_count": 0
    }

    from core.selector.selectorRegistry import SelectorRegistry
    reg = SelectorRegistry(storage_path="/dev/null")

    for key, bundles in data.items():
        ranks_seen = set()
        selectors_seen = set()

        for idx, b in enumerate(bundles):
            total_records += 1
            item_errors = []

            # Minimum Provenance Enforcement
            if not b.get("domain"): item_errors.append("Missing domain")
            if not b.get("page_signature_hash"): item_errors.append("Missing page_signature_hash")
            if not b.get("intent_type"): item_errors.append("Missing intent_type")
            if not b.get("selector_id"): item_errors.append("Missing selector_id")
            if not b.get("locator_type"): item_errors.append("Missing locator_type")
            if not b.get("locator_value"): item_errors.append("Missing locator_value")

            # Argument Schema explicit non-empty
            arg = b.get("argument_schema", "")
            if not arg or arg == "empty":
                item_errors.append("Empty argument schema")

            # Rank contiguous enforcement
            rank = b.get("rank")
            if rank in ranks_seen:
                item_errors.append(f"Duplicate Rank: {rank}")
            if rank != idx + 1:
                item_errors.append(f"Non-contiguous/Incorrect Rank expected {idx+1} got {rank}")
            ranks_seen.add(rank)

            # Selector duplication check
            s_id = b.get("selector_id")
            if s_id in selectors_seen:
                item_errors.append(f"Duplicate selector_id {s_id} in key chunk")
            selectors_seen.add(s_id)

            # Confidence bounds
            conf = float(b.get("confidence", -1))
            if conf < 0 or conf > 1.0:
                item_errors.append(f"Confidence out of bounds: {conf}")

            # Enum and RFC3339 constraints
            import re
            stat = b.get("stats", {})
            outcome = stat.get("last_outcome", "unknown")
            if outcome not in ["success", "fail", "unknown"]:
                item_errors.append("Invalid stats.last_outcome")
                counters["invalid_last_outcome_count"] += 1

            c_at = b.get("created_at", "")
            u_at = b.get("updated_at", "")
            rfc_match = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            if not rfc_match.match(c_at) or not rfc_match.match(u_at):
                item_errors.append("Invalid RFC3339 timestamp format")
                counters["non_rfc3339_timestamp_count"] += 1

            # Deterministic Recompute Check
            try:
                domain = b.get("domain", "")
                sig = b.get("page_signature_hash", "")
                intent = b.get("intent_type", "")
                recomputed_schema_hash = reg.hash_argument_schema(arg if type(arg)==str else reg.normalize_argument_schema(arg))

                # Assume stored key represents domain::sig::intent::schema_hash
                stored_parts = key.split("::")
                if len(stored_parts) == 4:
                    stored_hash = stored_parts[3]
                    if stored_hash != recomputed_schema_hash:
                        item_errors.append("arg_schema_hash_mismatch")
                        counters["arg_schema_hash_mismatch_count"] += 1

                    if len(stored_hash) != 64:
                        item_errors.append("invalid_hash_length")
                        counters["invalid_hash_length_count"] += 1

                    if not all(c in "0123456789abcdef" for c in stored_hash):
                        item_errors.append("invalid_hash_charset")
                        counters["invalid_hash_charset_count"] += 1

                expected_key = f"{domain}::{sig}::{intent}::{recomputed_schema_hash}"
                if expected_key != key:
                    item_errors.append("key_recompute_mismatch")
                    counters["key_recompute_mismatch_count"] += 1

            except Exception as e:
                item_errors.append(f"hash recomputation error: {e}")

            # Provenance Fingerprints
            reasons = b.get("reason_codes", [])
            if not reasons or len(reasons) == 0:
                item_errors.append("Empty reason_codes array")

            fingerprint = b.get("fingerprint", {})
            has_tag = "tag" in fingerprint
            has_text = "text_norm" in fingerprint
            valid_attr = any(attr in fingerprint for attr in ["data_testid", "data_qa", "id", "name", "aria_label"])
            if not (has_tag and has_text and valid_attr):
                item_errors.append("Fingerprint lacks required constraints (tag, text_norm, and 1 stable identifier)")

            if len(item_errors) > 0:
                err_dict = {
                    "key": key,
                    "locator_value": b.get("locator_value", "unknown"),
                    "violations": item_errors,
                    "record_count_checked": total_records
                }
                errors.append(err_dict)
                if len(invalid_samples) < 10:
                    invalid_samples.append({"violations": item_errors, "bundle": b})
            else:
                passed += 1

        # Evaluate group-level XPath assertions
        has_non_xpath = any(b.get("locator_type", "").lower() != "xpath" for b in bundles)
        for idx, b in enumerate(bundles):
            l_type = b.get("locator_type", "").lower()
            rank = b.get("rank", 1)
            # xpath_rank1 violation
            if l_type == "xpath" and has_non_xpath and rank == 1:
                errors.append({"key": key, "violations": ["xpath at rank 1 despite non-xpath existence"]})
                counters["xpath_rank1_violation_count"] += 1

            # xpath_only_without_flag
            if l_type == "xpath" and not has_non_xpath:
                if "XPATH_ONLY_ALLOWED_BY_FLAG" not in b.get("reason_codes", []):
                    errors.append({"key": key, "violations": ["xpath only group missing flag reason code"]})
                    counters["xpath_only_without_flag_count"] += 1

        # priority order violation check
        type_order = {"css": 1, "role": 2, "text": 3, "playwright": 4, "xpath": 5}
        for i in range(len(bundles) - 1):
            t1 = type_order.get(bundles[i].get("locator_type", "").lower(), 99)
            t2 = type_order.get(bundles[i+1].get("locator_type", "").lower(), 99)
            if t1 > t2:
                errors.append({"key": key, "violations": ["Locator Priority Type Sorting Rules Violated"]})
                counters["xpath_priority_order_violation_count"] += 1

    report = {
        "status": "PASS" if len(errors) == 0 else "FAIL",
        "total_records": total_records,
        "violation_count": len(errors),
        "errors": errors,
        "counters": counters
    }

    os.makedirs("artifacts", exist_ok=True)
    with open("artifacts/registry_validation_report.json", "w") as f:
        json.dump(report, f, indent=2)

    with open("artifacts/invalid_records_sample.json", "w") as f:
        json.dump(invalid_samples, f, indent=2)

    with open("artifacts/registry_validation_summary.txt", "w") as f:
        f.write(f"Registry Validation CI Summary\n")
        f.write(f"===============================\n")
        f.write(f"Total Records Scanned: {total_records}\n")
        f.write(f"Total Records Passed : {passed}\n")
        f.write(f"Total Violations     : {len(errors)}\n")
        f.write(f"Status: {report['status']}\n")

    if len(errors) > 0:
        print(f"FAIL: Registry validation failed with {len(errors)} violations.")
        sys.exit(1)
    else:
        print("PASS: Registry valid.")
        sys.exit(0)

if __name__ == "__main__":
    validate_registry_ci()
