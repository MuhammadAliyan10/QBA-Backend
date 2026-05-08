import time
import httpx
import logging
import asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeout
from .base_action import BaseAction
from ..context import ExecutionContext
from core.nervous_system import NervousSystem
from core.network_sniffer import NetworkSniffer

logger = logging.getLogger("action.login_and_sniff")

class LoginAndSniffAction(BaseAction):
    async def execute(self, ctx: ExecutionContext, payload: dict) -> dict:
        target_domain = payload.get("target_domain")
        url = payload.get("url")
        iterations = payload.get("iterations", 5)
        job_id = ctx.job_id
        page = ctx.page
        node_id = payload.get("_node_id", "unknown")

        sniffer = NetworkSniffer(target_domain=target_domain)
        await sniffer.start_sniffing(page)
        await NervousSystem.publish_update(job_id, "RUNNING", f"Sniffer monitoring {target_domain}...", node_id)

        await page.goto(url)

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            logger.debug("Network idle timeout during sniff phase (expected for streaming sites)")
        except Exception as e:
            logger.warning(f"Network wait failed during sniff: {e}")

        session = sniffer.get_session_context()

        if session:
            await NervousSystem.publish_update(job_id, "SUCCESS", "🔓 Golden Ticket Captured! Switching to Protocol Mode.", node_id)

            api_url = session["url"]
            headers = session["headers"]
            payload_template = session.get("payload")
            method = session["method"]

            async with httpx.AsyncClient() as client:
                start_time = time.time()
                for k in range(1, iterations + 1):
                    current_payload = payload_template

                    if isinstance(current_payload, dict):
                        current_payload = payload_template.copy()
                        if "page" in current_payload:
                            current_payload["page"] = int(current_payload["page"]) + k
                        elif "cursor" in current_payload:
                            current_payload["cursor"] = f"next_{k}"

                    try:
                        resp = await client.request(
                            method,
                            api_url,
                            headers=headers,
                            json=current_payload if isinstance(current_payload, dict) else None,
                            content=current_payload if isinstance(current_payload, str) else None,
                            timeout=10.0
                        )

                        duration = (time.time() - start_time) * 1000
                        size_kb = len(resp.content) / 1024

                        await NervousSystem.publish_update(
                            job_id, "RUNNING",
                            f"[Network] Protocol Hit #{k}: Status {resp.status_code} ({size_kb:.2f} KB) in {duration:.0f}ms",
                            node_id
                        )

                    except httpx.TimeoutException:
                        logger.warning(f"[Network] API timeout on replay #{k} - falling back to browser mode")
                        break
                    except httpx.HTTPError as e:
                        logger.warning(f"[Network] API error on replay #{k}: {e}")
                        break
                    except Exception as e:
                        logger.error(f"[Network] Unexpected error in protocol replay: {e}")
                        break

                    start_time = time.time()

            await NervousSystem.publish_update(job_id, "SUCCESS", f"Protocol Mode Complete: {iterations} requests replayed", node_id)

        else:
            msg = "No verified API keys found (Auth failed or no XHR). Continuing in Browser Mode."
            await NervousSystem.publish_update(job_id, "WARNING", msg, node_id)
            
        return {"success": True}
