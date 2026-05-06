# Core Browser Module

## Summary
The Browser module provides low-level interaction with Playwright. It manages sessions, executes raw actions, and implements stealth techniques for WAF evasion.

## File Manifest
* `action_map.py`: Maps standardized actions to Playwright methods. Inputs: Action enum. Outputs: Playwright function call.
* `dom_harvester.py`: JavaScript-injected script runner to serialize the DOM. Inputs: Page. Outputs: Cleaned DOM JSON.
* `extractor.js`: Raw JS payload for DOM extraction. Inputs: Window object. Outputs: JSON string.
* `state_signature.py`: Computes cryptographic hashes of the current DOM state to detect changes. Inputs: Page HTML. Outputs: State hash.
