# Stockwatch product tracker

Stockwatch is a local web dashboard for adding product links and tracking availability. It uses Playwright to inspect pages, SQLite to keep your watchlist between runs, and optional Telegram notifications when a product's known stock status changes.

The dashboard is monitor-only: it does not add products to carts, open checkout, or make purchases. The **₹4,800** amount is a reminder for your own review, not a payment control. The older `stock_agent.py --prepare-checkout` command is a separate, explicitly opted-in cart-staging flow; payment is still always manual.

## Start the web app

From the project directory, in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\playwright.exe install chromium
.\.venv\Scripts\python.exe web_app.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Paste an HTTPS product page link and choose **Track product**. The first check starts promptly; later checks use a five-minute interval. Use **Check now** to request an immediate check, or **Remove** to stop tracking it. Products are stored in `data/products.sqlite3`.

The monitor uses its own browser profile in `monitor_profile/`. Most public product pages do not need a login. If a store requires you to set a delivery pincode or sign in, stock status may remain unknown. The optional selector fields in **Advanced** can improve detection; use CSS selectors that identify the product's own stock indicator and price. Detection is best-effort across stores, so verify a result on the retailer's page.

The app only binds to `127.0.0.1` and uses a per-run token for its API. Keep it local; do not expose the port to your network. Stop it with Ctrl+C. Tracking runs only while the app and your computer are running.

## Set up Telegram alerts

1. In Telegram, open the official **@BotFather**, send `/newbot`, follow its prompts, and save the bot token somewhere private. Treat the token like a password.
2. Open your new bot's private chat and send `/start`. The bot cannot send a private message until you do this.
3. In PowerShell, get your personal chat ID. Replace the prompt's value by pasting your bot token; avoid sharing the output:

   ```powershell
   $token = Read-Host "Telegram bot token"
   $updates = Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/getUpdates"
   $updates.result[-1].message.chat.id
   ```

   If `result` is empty, send `/start` to the bot and run the command again. Use the `chat.id` from your own private chat, not a group.
4. In the **same PowerShell window** that you will use to launch Stockwatch, set the notification values:

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = Read-Host "Telegram bot token"
   $env:TELEGRAM_CHAT_ID = Read-Host "Telegram chat ID"
   .\.venv\Scripts\python.exe -c "from stock_agent import Notifier; Notifier().send('Stockwatch test notification')"
   ```

   Confirm the test arrives, then start `.\.venv\Scripts\python.exe web_app.py` in that same window. The monitor inherits these environment values. They are not written to the database or config. Without Telegram values, alerts are printed in the terminal.
5. To rotate a token, use BotFather and update the environment variable in the PowerShell window before restarting the app. Never paste the token into the product URL, a screenshot, or source code.

Stockwatch sends an alert when it first detects a known stock state and when that state changes. It does not notify repeatedly for an unchanged state.

## Detection and safety

- Product status prefers the page's matching Product structured data, then configured CSS selectors, then visible stock messages or a purchase button. If it cannot confidently determine availability, the last known state is kept and the dashboard shows a warning.
- Optional selectors are per-product and can be left blank. A price is only shown when one INR amount can be parsed; verify it manually.
- Product URLs must use HTTPS. Local/private IP addresses and checkout/payment paths are rejected. Navigations away from the product origin and common checkout/payment paths are blocked.
- The monitor profile is separate from the signed-in profile in `session_data/`. Do not point it at a trusted-account profile for arbitrary links; only track pages you trust.
- The app checks at most 100 products, with a minimum 60-second interval and a default five-minute interval. Follow the retailer's terms and reasonable request rates.
- Keep `monitor_profile/`, `session_data/`, `data/`, and Telegram credentials private. These local directories are ignored by Git.

## Existing login and cart-preparation tools

`auth_setup.py` prepares the separate `session_data/` profile. The single-product CLI in `stock_agent.py` reads `config.json`, and its optional `--prepare-checkout` flag can stage a product in a confirmed-empty cart after checking the configured amount. Store-specific selectors and cart layouts must be verified manually. It does not pay or place an order. This cart-preparation flow is not connected to the web dashboard.

Run the unit tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
