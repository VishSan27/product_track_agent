# Stock monitor and approval-only cart preparation

This Playwright agent checks one configured product page and reports stock changes. It can optionally stage one item in an **empty** cart, verify the displayed grand total is at most **₹4,800**, and send you the cart link. It never navigates to a payment flow, submits an order, or handles payment credentials. You inspect and approve checkout yourself.

The existing [`auth_setup.py`](./auth_setup.py) opens the signed-in browser profile at `session_data/`. Log in there first, then keep that profile private. The monitor opens the same persistent profile in a visible browser.

## Setup

1. Use a current Python version and install dependencies:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\playwright.exe install chromium
   ```

   If you are not using the existing `.venv`, replace `.\.venv\Scripts\python.exe` with `python` and run `python -m playwright install chromium`.
2. Log in to the store using `python auth_setup.py`. Complete any OTP or CAPTCHA yourself.
3. Copy `config.example.json` to `config.json`. Set the exact product URL and product name, then inspect the store page to replace the example CSS selectors. For cart preparation, set `cart_url` to the store's actual same-origin cart page. The `in_stock_selector` must identify an enabled control/indicator only when that product can be bought. Selectors are store-specific; do not assume the examples are valid.
4. To receive Telegram alerts, create a private bot/chat and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the process environment. Never put these values in `config.json` or source control. Without Telegram settings, messages are printed locally.

For example, set secrets only in the current PowerShell window:

```powershell
$env:TELEGRAM_BOT_TOKEN = "your-token"
$env:TELEGRAM_CHAT_ID = "your-chat-id"
```

## Run

Monitor continuously (5-minute interval by default):

```powershell
.\.venv\Scripts\python.exe stock_agent.py --config config.json
```

Run one check:

```powershell
.\.venv\Scripts\python.exe stock_agent.py --config config.json --once
```

Opt in to cart preparation only after configuring and testing every cart selector:

```powershell
.\.venv\Scripts\python.exe stock_agent.py --config config.json --prepare-checkout
```

The agent refuses to stage unless it confirms the cart is empty first. Avoid editing the cart in another tab while staging. It checks the product price before adding, then requires exactly one cart line and reads the configured cart grand-total selector after adding. If the total is above ₹4,800, unknown, ambiguous, or the cart state is unexpected, it does not send an actionable checkout link. If the item was already added when this is discovered, remove it from the cart manually. The store's configured total selector must include taxes, delivery, and other mandatory charges for the cap to mean what you expect. The final amount can still change; verify it at checkout.

The cart link points to the store's cart in the monitored browser profile. It may not work in a phone app or a different browser session. Do not treat it as approval or authorization to pay.

## Safety architecture

- **Persistent account state:** Playwright uses the local `session_data/` profile created by `auth_setup.py`; the profile is excluded from Git. Never share or upload it.
- **Read-only monitoring by default:** `--prepare-checkout` is required for cart writes. Stock status is based on explicit configured selectors; missing/ambiguous status is reported in logs as unknown.
- **Fail-closed purchase staging:** HTTPS-only URLs, same-origin cart URL, minimum 30-second polling, empty-cart precondition, single-item check, exact price parsing, and ₹4,800 upper bound. Unknown or invalid prices are errors, never treated as zero.
- **Human payment approval:** No checkout/payment URL is constructed, no payment page is opened, and no purchase/payment action is implemented. You review the cart and complete payment yourself.
- **Secret and notification handling:** Telegram credentials are environment-only; Telegram sends product status and the cart URL. Restrict the bot/chat to yourself. Do not expose logs or browser-profile data.
- **Operational limits:** Use only where permitted by the store's terms and robots/rate-limit guidance. The interval is configurable but cannot be set below 30 seconds. Stop with Ctrl+C.

## Implementation plan

1. Configure and verify the product and stock selectors on the store page.
2. Run monitor-only mode, first with `--once`, and confirm status/notifications.
3. Configure the cart selectors; verify the empty-cart and grand-total selectors manually.
4. Run `--prepare-checkout` with an empty cart and confirm the resulting notification. Keep payment manual for every order.
5. Add a Windows Task Scheduler job only after interactive runs work; use the same Windows account/profile and protect the Telegram environment variables.

Run the unit tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
