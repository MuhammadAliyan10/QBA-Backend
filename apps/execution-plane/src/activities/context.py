import logging
from dataclasses import dataclass
from playwright.async_api import Page, BrowserContext
from core.selector.smart_finder import SmartFinder
from core.user_facing_logger import UserFriendlyLogger
from core.network_sniffer import NetworkSniffer

@dataclass
class ExecutionContext:
    job_id: str
    page: Page
    browser_context: BrowserContext
    finder: SmartFinder
    user_logger: UserFriendlyLogger
    global_sniffer: NetworkSniffer
