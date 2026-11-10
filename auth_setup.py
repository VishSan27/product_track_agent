import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = Path(__file__).resolve().parent / "session_data"


async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False
        )
        
        # Use the initial page rather than creating a duplicate tab
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://shop.amul.com/en/")
        
        print("\n" + "=" * 50)
        print("1. Log in manually in the opened browser window.")
        print("2. Solve any OTP/CAPTCHA required.")
        print("3. Return here and press ENTER to save session and exit.")
        print("=" * 50 + "\n")
        
        # Keeps the browser open indefinitely until you press Enter in terminal
        await asyncio.to_thread(input, "Press ENTER when logged in: ")
        
        await context.close()
        print(f"Session saved successfully to {PROFILE_DIR}")

asyncio.run(main())