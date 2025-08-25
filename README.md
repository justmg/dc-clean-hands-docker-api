# DC Clean Hands Browser-Use Agent

Automates the DC MyTax "Validate a Certificate of Clean Hands" workflow using the browser-use library (Playwright backend on v0.5.5).

- Navigates to https://mytax.dc.gov/_/
- Clicks "Validate a Certificate of Clean Hands"
- Fills Notice number and last 4 digits
- Clicks Search, extracts compliance status
- Takes a screenshot
- Requests current certificate, goes Next → Submit
- Opens the PDF "View certificate" and saves it locally

Artifacts (screenshots, PDFs) are saved to `artifacts/`.

## Prerequisites
- Python 3.11+
- An OpenAI API key (for `ChatOpenAI` used by browser-use)

## Setup (Windows PowerShell)
```powershell
# 1) Create and activate venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) Install dependencies
pip install -r requirements.txt

# 3) Install Playwright browsers
python -m playwright install chromium

# 4) Create a .env (optional) and set variables
Copy-Item .env.example .env
# Edit .env to put your OPENAI_API_KEY

# Alternatively set in-session env vars (PowerShell):
$env:OPENAI_API_KEY = "sk-..."
$env:NOTICE = "L0014500721"
$env:L4 = "0257"

# 5) Run the agent
python .\mytaxdc_agent.py
```

## Configuration
- `.env`:
  - `OPENAI_API_KEY` (required)
  - `MODEL_NAME` (optional, default `gpt-4.1-mini`)
  - `NOTICE`, `L4` (optional; defaults are provided in the script)

- Headless mode is disabled by default for easier debugging. Change in `mytaxdc_agent.py` by setting:
  ```python
  browser_profile = BrowserProfile(headless=True)
  ```

## Outputs
- Screenshot saved to `artifacts/clean-hands-<NOTICE>-<timestamp>.png`
- PDF saved to `artifacts/clean-hands-<NOTICE>-<timestamp>.pdf` (when available)
- The script prints the final JSON result and visited URLs to stdout.

## Notes
- This project pins `browser-use==0.5.5` to ensure the Playwright-based APIs remain stable.
- Selectors are robust (role/text/label/placeholder fallbacks) but the DC site can change. If it does, update the locators in `clean_hands_workflow()` inside `mytaxdc_agent.py`.
