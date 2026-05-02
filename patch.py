import re

def fix_activities():
    with open('apps/execution-plane/src/activities/activities.py', 'r') as f:
        lines = f.readlines()

    new_lines = []
    in_execution_engine = False
    
    for i, line in enumerate(lines):
        # Initial wait for navigation and loop resilience
        if "if action == \"GOTO\":" in line:
            pass # Keep track of where we are
            
        if "# --- GHOST SESSION VERIFICATION & AUTH LOCK ---" in line:
            in_execution_engine = True

        if in_execution_engine and "except Exception as e:" in line and i + 1 < len(lines) and "raise e" in lines[i+1]:
            in_execution_engine = False

        if in_execution_engine:
            # Add 4 spaces of indentation
            line = "    " + line
            
            # Initial navigation injection before execution loop
            if "# --- 6. EXECUTION LOOP ---" in line:
                new_lines.append("""
                # --- PRE-LOOP: Mandatory initial navigation ---
                if target_url:
                    logger.info(f"[{job_id}] Pre-loop navigation to {target_url}")
                    try:
                        await page.goto(target_url, timeout=NAVIGATION_TIMEOUT, wait_until="networkidle")
                        # Settle JS redirects
                        await asyncio.sleep(2)
                    except Exception as nav_err:
                        logger.warning(f"[{job_id}] Pre-loop navigation error (non-fatal): {nav_err}")
""")

            # Wrap the DOM extraction with try/except in EXTRACT block
            # Actually the prompt says: "Re-apply Resilience: Wrap the initial DOM extraction inside the execution loop with a try/except playwright.async_api.Error."
            # Wait, the prompt specifically mentions: "Wrap the initial DOM extraction inside the execution loop with a try/except playwright.async_api.Error. Implement a 3-retry limit with asyncio.sleep(2) if the context crashes"
        
        new_lines.append(line)

    with open('apps/execution-plane/src/activities/activities.py', 'w') as f:
        f.writelines(new_lines)

fix_activities()
