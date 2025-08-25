# app.py
# Single-file FastAPI service for DC MyTax "Clean Hands" check + CloudMailin email (with PDF attachment).
# Deployable on Heroku. Exposes POST /clean-hands that returns {status, notice, last4} and
# sends an email (in the background) with the PDF if available.

import os
import re
import json
import time
import base64
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, EmailStr, Field
from dotenv import load_dotenv
import httpx

# Playwright & browser_use
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from browser_use import BrowserProfile, BrowserSession, ActionResult

# --------------------------------------------------------------------------------------
# Environment / constants
# --------------------------------------------------------------------------------------

load_dotenv()

# Set Playwright environment variables for Heroku
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/.cache/ms-playwright")
# Force browser-use to skip Playwright browser downloads
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# CloudMailin Outbound API configuration (set in Heroku Config Vars)
CLOUDMAILIN_SMTP_USERNAME = os.getenv("CLOUDMAILIN_SMTP_USERNAME")  # required
CLOUDMAILIN_API_TOKEN = os.getenv("CLOUDMAILIN_API_TOKEN")          # required
FROM_EMAIL = os.getenv("FROM_EMAIL", "no-reply@example.com")
FROM_NAME = os.getenv("FROM_NAME", "Clean Hands Bot")
CLOUDMAILIN_API_BASE = "https://api.cloudmailin.com/api/v0.1"

# --------------------------------------------------------------------------------------
# Request model
# --------------------------------------------------------------------------------------

class CleanHandsRequest(BaseModel):
    notice: str = Field(..., min_length=5, max_length=64, description="Notice number")
    last4: str = Field(..., pattern=r"^\d{4}$", description="Last 4 digits")
    email: EmailStr

# --------------------------------------------------------------------------------------
# Core workflow (Playwright implemented using browser_use BrowserSession)
# --------------------------------------------------------------------------------------

async def clean_hands_workflow(
    notice: str,
    last4: str,
    browser_session: BrowserSession,
) -> ActionResult:
    """
    Deterministic workflow for DC MyTax Clean Hands:
      1) https://mytax.dc.gov/_/
      2) "Validate a Certificate of Clean Hands"
      3) Fill notice + last 4 → Search
      4) Detect compliance status
      5) Request current certificate (Next → Submit)
      6) View Certificate (PDF) and save under artifacts/
    Returns ActionResult with JSON payload in extracted_content and is_done=True
    """
    result: dict = {
        "status": "unknown",
        "message": "",
        "screenshot_path": None,
        "pdf_path": None,
        "urls": [],
        "notice": notice,
        "last4": last4,
    }

    ts = int(time.time())

    def add_url(u: Optional[str]):
        if u:
            result["urls"].append(u)

    page = await browser_session.get_current_page()

    # Step 1: open site
    try:
        await page.goto("https://mytax.dc.gov/_/", wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_load_state("networkidle")
        add_url(page.url)
    except Exception as e:
        return ActionResult(error=f"Failed to open site: {type(e).__name__}: {e}", is_done=True)

    # Step 2: click "Validate a Certificate of Clean Hands"
    try:
        link = page.get_by_role("link", name=re.compile(r"Validate.*Clean\s*Hands", re.I))
        if not await link.count():
            link = page.get_by_text(re.compile(r"Validate a Certificate of Clean Hands", re.I), exact=False)

        await link.first.click(timeout=30_000)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle")
        add_url(page.url)
    except Exception:
        try:
            await page.mouse.wheel(0, 2000)
            link = page.get_by_role("link", name=re.compile(r"Validate.*Clean\s*Hands", re.I))
            if not await link.count():
                link = page.get_by_text(re.compile(r"Validate a Certificate of Clean Hands", re.I), exact=False)
            await link.first.click(timeout=30_000)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_load_state("networkidle")
            add_url(page.url)
        except Exception as e:
            return ActionResult(
                error=f"Failed to find/click 'Validate a Certificate of Clean Hands': {type(e).__name__}: {e}",
                is_done=True,
            )

    # Step 3–4: fill form and search
    try:
        notice_input = page.locator("input").nth(0)
        last4_input = page.locator("input").nth(1)

        await notice_input.wait_for(state="visible", timeout=15_000)
        await notice_input.fill(notice, timeout=10_000)

        await last4_input.wait_for(state="visible", timeout=15_000)
        await last4_input.fill(last4, timeout=10_000)

        btn = page.get_by_role("button", name=re.compile(r"^Search$", re.I))
        if not await btn.count():
            btn = page.locator('button:has-text("Search"), input[type="submit"][value*="Search" i]')

        if await btn.count():
            await btn.first.click(timeout=20_000)
        else:
            await last4_input.press("Enter")

        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_load_state("networkidle")
    except Exception as e:
        return ActionResult(error=f"Failed to fill form/search: {type(e).__name__}: {e}", is_done=True)

    # Step 5: detect status
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        body_text = None

    status = "unknown"
    if body_text:
        if re.search(r"\bnon[- ]?compliant\b", body_text, re.I):
            status = "noncompliant"
        elif re.search(r"\bcompliant\b", body_text, re.I):
            status = "compliant"

    result["status"] = status
    result["message"] = "Detected compliance status from page." if status != "unknown" else "Could not detect compliance status."

    # Step 6: request current certificate (Next → Submit) and save PDF
    try:
        req_link = page.get_by_role(
            "link",
            name=re.compile(r"Click here to request.*Certificate of Clean Hands", re.I),
        )
        if not await req_link.count():
            req_link = page.get_by_text(
                re.compile(r"Click here to request a current Certificate of Clean Hands", re.I), exact=False
            )

        if await req_link.count():
            await req_link.first.click(timeout=30_000)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_load_state("networkidle")
            add_url(page.url)

            next_btn = page.get_by_role("button", name=re.compile(r"^Next$", re.I))
            if await next_btn.count():
                await next_btn.first.click(timeout=20_000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_load_state("networkidle")
                add_url(page.url)

            submit_btn = page.get_by_role("button", name=re.compile(r"Submit", re.I))
            if await submit_btn.count():
                await submit_btn.first.click(timeout=30_000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_load_state("networkidle")
                add_url(page.url)

        view_link = page.get_by_role("link", name=re.compile(r"view certificate", re.I))
        if not await view_link.count():
            view_link = page.get_by_text(re.compile(r"view certificate", re.I), exact=False)

        pdf_path: Optional[str] = None
        if await view_link.count():
            # Prefer direct download
            try:
                async with page.expect_download(timeout=20_000) as dl_info:
                    await view_link.first.click()
                download = await dl_info.value
                pdf_path = str(ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf")
                await download.save_as(pdf_path)
            except Exception:
                # Fallback: popup
                try:
                    async with page.expect_popup(timeout=10_000) as pop_info:
                        await view_link.first.click()
                    popup = await pop_info.value
                    await popup.wait_for_load_state("load")
                    add_url(popup.url)

                    def is_pdf(resp):
                        # playwright Response.headers is a dict
                        ct = (resp.headers or {}).get("content-type", "").lower()
                        return "application/pdf" in ct

                    try:
                        resp = await popup.wait_for_event("response", predicate=is_pdf, timeout=10_000)
                        content = await resp.body()
                    except PlaywrightTimeoutError:
                        # Last resort: fetch current URL within page context to keep cookies
                        content = await popup.evaluate(
                            """
                            async () => {
                                const res = await fetch(location.href, { credentials: 'include' });
                                const ab = await res.arrayBuffer();
                                return Array.from(new Uint8Array(ab));
                            }
                            """
                        )
                        content = bytes(content) if isinstance(content, list) else content

                    if content:
                        pdf_path = str(ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf")
                        with open(pdf_path, "wb") as f:
                            f.write(content)
                except Exception:
                    pass

        if pdf_path:
            result["pdf_path"] = pdf_path

    except Exception:
        # Non-fatal: page variants are common
        pass

    return ActionResult(extracted_content=json.dumps(result), is_done=True)

# --------------------------------------------------------------------------------------
# Email (CloudMailin Outbound JSON API)
# --------------------------------------------------------------------------------------

async def send_cloudmailin_email(
    to_email: str,
    subject: str,
    plain_text: str,
    html_body: str,
    attachment_path: Optional[str] = None,
) -> None:
    """
    POST https://api.cloudmailin.com/api/v0.1/{SMTP_USERNAME}/messages
    Authorization: Bearer <API_TOKEN>
    Attachments base64-encoded with content_type 'application/pdf'.
    """
    if not (CLOUDMAILIN_SMTP_USERNAME and CLOUDMAILIN_API_TOKEN):
        return  # silently skip if not configured

    attachments = []
    if attachment_path and Path(attachment_path).exists():
        data = Path(attachment_path).read_bytes()
        attachments.append({
            "file_name": Path(attachment_path).name,
            "content": base64.b64encode(data).decode("ascii"),
            "content_type": "application/pdf",
            "content_id": None
        })

    payload = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "plain": plain_text,
        "html": html_body,
        "attachments": attachments,
    }

    url = f"{CLOUDMAILIN_API_BASE}/{CLOUDMAILIN_SMTP_USERNAME}/messages"
    headers = {"Authorization": f"Bearer {CLOUDMAILIN_API_TOKEN}"}

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()  # let platform logs capture failures

# --------------------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------------------

app = FastAPI(title="DC Clean Hands API (Heroku single-file)")

@app.get("/_health")
async def health():
    return {"ok": True}

@app.post("/clean-hands")
async def run_clean_hands(req: CleanHandsRequest, bg: BackgroundTasks):
    """
    Body: { "notice": "...", "last4": "0257", "email": "user@example.com" }
    Returns: { "status": "compliant|noncompliant|unknown", "notice", "last4", "email_enqueued": true }
    Sends email (in background) via CloudMailin with attached PDF if available.
    """
    # Configure browser profile with system Chromium for Heroku
    chrome_paths = [
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable", 
        "/usr/bin/google-chrome",
        "/snap/bin/chromium",
        "/app/.cache/ms-playwright/chromium-1181/chrome-linux/chrome"
    ]
    
    chrome_path = None
    for path in chrome_paths:
        if Path(path).exists():
            chrome_path = path
            print(f"Found Chrome at: {chrome_path}")
            break
    
    if not chrome_path:
        print("No Chrome executable found, using system default")
        
    profile = BrowserProfile(
        headless=True, 
        downloads_path=str(ARTIFACTS_DIR),
        executable_path=chrome_path,
        # Override browser type to use system Chrome instead of Playwright
        browser="chromium" if chrome_path and "chromium" in chrome_path else "chrome",
        # Add Heroku-friendly Chrome options
        extra_chromium_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage", 
            "--disable-gpu",
            "--disable-web-security",
            "--disable-features=VizDisplayCompositor",
            "--remote-debugging-port=9222"
        ]
    )
    session = BrowserSession(browser_profile=profile)

    try:
        ar: ActionResult = await clean_hands_workflow(
            notice=req.notice,
            last4=req.last4,
            browser_session=session,
        )
    finally:
        try:
            await session.close()
        except Exception:
            pass

    if ar.error:
        raise HTTPException(status_code=500, detail=ar.error)

    data = {}
    if ar.extracted_content:
        try:
            data = json.loads(ar.extracted_content)
        except Exception:
            pass

    status = data.get("status", "unknown")
    pdf_path = data.get("pdf_path")

    subject = f"DC Clean Hands Certificate – {req.notice} ({status})"
    plain = f"Compliance status: {status}\nNotice: {req.notice}\nLast4: {req.last4}\n"
    html = f"""
        <p><strong>Compliance status:</strong> {status}</p>
        <p><strong>Notice:</strong> {req.notice}<br/>
        <strong>Last4:</strong> {req.last4}</p>
        <p>The certificate is attached if it was available.</p>
    """

    bg.add_task(
        send_cloudmailin_email,
        req.email,
        subject,
        plain,
        html,
        pdf_path,
    )

    return {
        "status": status,
        "notice": req.notice,
        "last4": req.last4,
        "email_enqueued": True,
    }

# --------------------------------------------------------------------------------------
# Local dev entrypoint (Heroku uses gunicorn via Procfile)
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("singlefile:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
