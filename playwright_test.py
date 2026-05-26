"""Playwright smoke test:
  1) Try chat.deepseek.com (informational — usually 403 against headless / requires login).
  2) Wikipedia: open the site, search for 'DeepSeek', read the article's first paragraph.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path("playwright_out")
OUT.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800},
                                        user_agent=UA, locale="en-US")

        # 1) DeepSeek reachability check (just to be honest about why we don't 'ask a question').
        print("[1] Probing chat.deepseek.com ...")
        page = await ctx.new_page()
        try:
            resp = await page.goto("https://chat.deepseek.com",
                                   wait_until="domcontentloaded", timeout=20000)
            status = resp.status if resp else "no-response"
            title = await page.title()
            print(f"    HTTP {status} | title: {title!r}")
            await page.screenshot(path=str(OUT / "deepseek.png"))
        except Exception as e:
            print(f"    failed: {e}")
        await page.close()

        # 2) Real interaction on Wikipedia (fresh page, no interference from #1).
        print("[2] Wikipedia: open homepage, type 'DeepSeek', submit ...")
        page = await ctx.new_page()
        await page.goto("https://en.wikipedia.org/wiki/Main_Page",
                        wait_until="domcontentloaded", timeout=30000)
        await page.fill("input[name='search']", "DeepSeek")
        await page.keyboard.press("Enter")
        await page.wait_for_selector("#firstHeading", timeout=20000)

        heading = (await page.inner_text("#firstHeading")).strip()
        # First non-empty paragraph of the article body.
        paragraphs = await page.locator("#mw-content-text p").all_inner_texts()
        first_para = next((p.strip() for p in paragraphs if p.strip()), "")
        print(f"    article: {heading!r}")
        print(f"    first paragraph ({len(first_para)} chars):")
        print(f"      {first_para[:400]}{'...' if len(first_para) > 400 else ''}")

        await page.screenshot(path=str(OUT / "wikipedia_deepseek.png"), full_page=False)

        await ctx.close()
        await browser.close()
        print("[done] Browser closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
