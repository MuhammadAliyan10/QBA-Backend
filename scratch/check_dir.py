import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        print([x for x in dir(page) if 'access' in x.lower() or 'aom' in x.lower() or 'snap' in x.lower()])
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
