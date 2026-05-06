import os
import json
import sys
from typing import Dict, Any

def migrate_registry(path=".quanta_registry.json"):
    if not os.path.exists(path):
        print("No registry to migrate.")
        return

    with open(path, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print("Invalid JSON.")
            return

    new_data = {}
    quarantine = []

    # Needs to rebuild canonical keys and clean out empty argument schemas
    from core.selector.selectorRegistry import SelectorRegistry, SelectorBundle
    reg = SelectorRegistry(storage_path="/dev/null")

    scanned = 0
    repaired = 0

    for old_key, bundles in data.items():
        for b in bundles:
            scanned += 1
            # Infer logic:
            domain = b.get("domain", "unknown_domain")
            sig = b.get("page_signature_hash", "default_hash")
            intent = b.get("intent_type", b.get("intent", "unknown_intent"))
            arg = b.get("argument_schema", "")

            if not arg or arg == "empty" or arg == "":
                quarantine.append({"reason": "empty_argument", "bundle": b})
                continue

            try:
                # If arg is just a string, try hash it
                if type(arg) == str and ":" in arg:
                    schema_hash = reg.hash_argument_schema(arg)
                elif type(arg) == dict:
                    arg_schema = reg.normalize_argument_schema(arg)
                    schema_hash = reg.hash_argument_schema(arg_schema)
                else:
                    raise ValueError("Cannot parse missing type argument schema")
            except Exception as e:
                quarantine.append({"reason": f"invalid_schema: {e}", "bundle": b})
                continue

            new_key = f"{domain}::{sig}::{intent}::{schema_hash}"

            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Enum normalize
            stats = b.get("stats", {"success_count": 0, "fail_count": 0, "last_outcome": "unknown"})
            if stats.get("last_outcome") not in ["success", "fail", "unknown"]:
                stats["last_outcome"] = "unknown"

            # Parse Timestamps cleanly
            c_at = b.get("created_at", "0")
            u_at = b.get("updated_at", "0")
            if str(c_at).isdigit() and len(str(c_at)) <= 10:
                # epoch seconds to RFC3339
                try:
                    c_at = datetime.fromtimestamp(int(c_at), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except: c_at = now_utc
            elif "T" not in c_at:
                c_at = now_utc

            if str(u_at).isdigit() and len(str(u_at)) <= 10:
                try:
                    u_at = datetime.fromtimestamp(int(u_at), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except: u_at = now_utc
            elif "T" not in u_at:
                u_at = now_utc

            new_bundle = SelectorBundle(
                key=new_key,
                domain=domain,
                page_signature_hash=sig,
                intent_type=intent,
                argument_schema=arg if type(arg)==str else arg_schema,
                selector_id=b.get("selector_id", "unnamed_id"),
                locator_type=b.get("locator_type", "css"),
                locator_value=b.get("locator_value", ""),
                confidence=b.get("confidence", 1.0),
                reason_codes=b.get("reason_codes", ["MIGRATED"]),
                fingerprint=b.get("fingerprint", {"tag": "div", "text_norm": "migrated", "id": "migrated"}),
                stats=stats,
                created_at=c_at,
                updated_at=u_at,
                version=b.get("version", 1)
            )

            # Re-rank execution handles duplicates
            if new_key not in new_data:
                new_data[new_key] = []

            existing = new_data[new_key]

            # Avoid direct duplication
            found = False
            for ex in existing:
                if ex.locator_value == new_bundle.locator_value:
                    found = True
                    break

            if not found:
                existing.append(new_bundle)
                repaired += 1

    # Reranking logic applying deterministic arrays
    from config import is_xpath_only_allowed
    allow_xpath = is_xpath_only_allowed()

    final_output = {}
    for key, bundles in new_data.items():
        has_non_xpath = any(b.locator_type.lower() != "xpath" for b in bundles)

        valid_bundles_for_key = []
        for b in bundles:
            is_only_xpath = b.locator_type.lower() == "xpath" and not has_non_xpath
            if is_only_xpath and not allow_xpath:
                quarantine.append({"reason": "xpath_only_prohibited", "bundle": b.to_dict()})
                repaired -= 1
                continue
            elif is_only_xpath and allow_xpath:
                if "XPATH_ONLY_ALLOWED_BY_FLAG" not in b.reason_codes:
                    b.reason_codes.append("XPATH_ONLY_ALLOWED_BY_FLAG")

            valid_bundles_for_key.append(b)

        if valid_bundles_for_key:
            sorted_bundles = reg._auto_rerank(valid_bundles_for_key)
            final_output[key] = [x.to_dict() for x in sorted_bundles]

    with open(".quanta_registry.json", "w") as f:
        json.dump(final_output, f, indent=2)

    with open(".quanta_quarantine.json", "w") as f:
        json.dump(quarantine, f, indent=2)

    os.makedirs("artifacts", exist_ok=True)
    report = {
        "scanned": scanned,
        "repaired": repaired,
        "quarantined": len(quarantine)
    }
    with open("artifacts/migration_report.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"Migration completed. Scanned: {scanned}, Repaired: {repaired}, Quarantined: {len(quarantine)}")

if __name__ == "__main__":
    migrate_registry()
