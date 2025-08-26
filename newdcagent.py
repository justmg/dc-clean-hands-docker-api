import argparse
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Literal, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError

# ---------------------------
# Configuration & Logging
# ---------------------------
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("mytaxdc")

# Global timeouts (ms)
NAV_TIMEOUT = 60_000
LONG_TIMEOUT = 300_000
SHORT_TIMEOUT = 10_000


@dataclass
class WorkflowResult:
    status: Literal["compliant", "noncompliant", "unknown"]
    message: str
    screenshot_path: Optional[str]
    pdf_path: Optional[str]
    urls: List[str]
    notice: str
    last4: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ---------------------------
# PDF Detection & Utilities
# ---------------------------
def looks_like_pdf_url(url: str) -> bool:
    u = (url or "").lower()
    return u.endswith(".pdf") or ("/retrieve/" in u and "file__=" in u)


def is_pdf_like_headers(ct: Optional[str], url: Optional[str]) -> bool:
    ct = (ct or "").lower()
    u = (url or "").lower()
    return (
        "application/pdf" in ct
        or "application/octet-stream" in ct
        or "application/force-download" in ct
        or looks_like_pdf_url(u)
    )


async def force_download_via_anchor(page: Page, url: str, out_path: Path) -> Optional[str]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with page.expect_download(timeout=LONG_TIMEOUT) as dl_info:
            await page.evaluate(
                """(url) => {
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'document.pdf';
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                }""",
                url,
            )
        download = await dl_info.value
        await download.save_as(str(out_path))
        logger.info(f"[force-anchor] Downloaded -> {out_path}")
        return str(out_path)
    except Exception as e:
        logger.debug(f"[force-anchor] failed: {e}")
        return None


async def force_download_via_blob(page: Page, url: str, out_path: Path) -> Optional[str]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with page.expect_download(timeout=LONG_TIMEOUT) as dl_info:
            await page.evaluate(
                """async (url) => {
                    const res  = await fetch(url, { credentials: 'include' });
                    if (!res.ok) throw new Error('fetch failed ' + res.status);
                    const blob = await res.blob();
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(blob);
                    link.download = 'document.pdf';
                    document.body.appendChild(link);
                    link.click();
                    setTimeout(() => {
                        URL.revokeObjectURL(link.href);
                        link.remove();
                    }, 5000);
                }""",
                url,
            )
        download = await dl_info.value
        await download.save_as(str(out_path))
        logger.info(f"[force-blob] Downloaded -> {out_path}")
        return str(out_path)
    except Exception as e:
        logger.debug(f"[force-blob] failed: {e}")
        return None


async def download_via_context_request(context: BrowserContext, url: str, out_path: Path) -> Optional[str]:
    logger.info(f"[context-request] GET {url}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = await context.request.get(
            url,
            headers={
                "Accept": "application/pdf,application/octet-stream,*/*",
                "Referer": "https://mytax.dc.gov/_/",
            },
            timeout=LONG_TIMEOUT / 1000,  # seconds
        )
        if not resp.ok:
            logger.warning(f"[context-request] {url} failed: {resp.status}")
            return None
        data = await resp.body()
        if not data:
            logger.warning("[context-request] response had no body")
            return None
        with open(out_path, "wb") as f:
            f.write(data)
        logger.info(f"[context-request] Saved -> {out_path} ({len(data)} bytes)")
        return str(out_path)
    except Exception as e:
        logger.debug(f"[context-request] error: {e}")
        return None


async def attach_pdf_route_capture(context: BrowserContext, out_path: Path, state: dict):
    """
    Intercept the actual PDF network request and persist bytes via route.fetch().
    Context-level route captures all pages/popups.
    """
    if state.get("_route_attached"):
        return
    state["_route_attached"] = True

    async def handler(route):
        if state.get("saved"):
            await route.continue_()
            return
        req = route.request
        url = req.url or ""
        if looks_like_pdf_url(url):
            try:
                resp = await route.fetch()
                body = await resp.body()
                if body:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "wb") as f:
                        f.write(body)
                    state["saved"] = True
                    state["path"] = str(out_path)
                    logger.info(f"[route] Saved PDF via route: {out_path} ({len(body)} bytes) from {url}")
                # Always fulfill to keep browser behavior intact
                await route.fulfill(status=resp.status, headers=dict(resp.headers), body=body)
            except Exception as e:
                logger.warning(f"[route] error for {url}: {e}")
                await route.continue_()
        else:
            await route.continue_()

    await context.route("**/*", handler)
    logger.info("[route] PDF route capture attached at context level")


def pick_pdf_url_from_history(urls: List[str], current_url: Optional[str]) -> Optional[str]:
    for u in reversed(urls or []):
        if looks_like_pdf_url(u):
            return u
    if current_url and looks_like_pdf_url(current_url):
        return current_url
    return None


async def harvest_from_pages(pages: List[Page], out_path: Path, state: dict):
    """If any page currently shows a PDF, fetch bytes from its URL inside the page context."""
    if state.get("saved"):
        return state["path"]
    for p in pages:
        try:
            url = p.url
            if looks_like_pdf_url(url):
                logger.info(f"[harvest] Found open PDF tab: {url}")
                bytes_list = await p.evaluate(
                    """
                    async () => {
                        const res = await fetch(window.location.href, { credentials: 'include' });
                        if (!res.ok) throw new Error('fetch failed ' + res.status);
                        const ab  = await res.arrayBuffer();
                        return Array.from(new Uint8Array(ab));
                    }
                """
                )
                if bytes_list:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "wb") as f:
                        f.write(bytes(bytes_list))
                    state["saved"] = True
                    state["path"] = str(out_path)
                    logger.info(f"[harvest] Saved PDF -> {out_path}")
                    return state["path"]
        except Exception as e:
            logger.debug(f"[harvest] {e}")
    return None


# ---------------------------
# Page Interaction Helpers
# ---------------------------
async def maybe_click(page: Page, locator) -> bool:
    try:
        count = await locator.count()
        if count > 0:
            await locator.first.click(timeout=SHORT_TIMEOUT)
            return True
    except Exception as e:
        logger.debug(f"maybe_click: {e}")
    return False


async def save_screenshot(page: Page, dest: Path, enable: bool) -> Optional[str]:
    if not enable:
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(dest), full_page=True)
        return str(dest)
    except Exception as e:
        logger.debug(f"save_screenshot failed: {e}")
        return None


async def handle_security_warning(page: Page) -> None:
    try:
        elements = [
            page.get_by_role("link", name=re.compile(r"Click\s*Here\s*to\s*Start\s*Over", re.I)),
            page.get_by_text(re.compile(r"Click\s*Here\s*to\s*Start\s*Over", re.I), exact=False),
        ]
        for el in elements:
            if await maybe_click(page, el):
                await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                break
    except Exception as e:
        logger.debug(f"No security warning handled (ok): {e}")


def detect_status_from_text(text: str) -> Literal["compliant", "noncompliant", "unknown"]:
    # Key insight: If page offers to "request a Notice of Non-Compliance", 
    # it means the entity is currently COMPLIANT (otherwise they'd already have the notice)
    
    # Check for explicit compliance first (handle common page phrasing)
    if re.search(r"\bthis\s+taxpayer\s+is\s+currently\s+compliant\b", text, re.I):
        return "compliant"
    if re.search(r"\bin\s+compliance\b", text, re.I):
        return "compliant"
    if re.search(r"\bis\s+compliant\b", text, re.I):
        return "compliant"
    
    # Check if page offers to request non-compliance notice (means currently compliant)
    if re.search(r"request.*notice.*non[-\s]?compliance", text, re.I):
        return "compliant"
    if re.search(r"click here to request.*non[-\s]?compliance", text, re.I):
        return "compliant"
    
    # Check for explicit non-compliance status
    if re.search(r"\bnot\s+in\s+compliance\b", text, re.I):
        return "noncompliant"
    if re.search(r"\bis\s+not\s+compliant\b", text, re.I):
        return "noncompliant"
    if re.search(r"\bnot\s+compliant\b", text, re.I):
        return "noncompliant"
    
    # Generic compliant check (but avoid false positives from "non-compliant" / "noncompliance")
    if (
        re.search(r"\bcompliant\b", text, re.I)
        and not re.search(r"\bnon[-\s]?compliant\b", text, re.I)
        and not re.search(r"\bnon[-\s]?compliance\b", text, re.I)
    ):
        return "compliant"
    
    return "unknown"


async def click_validate_link(page: Page) -> None:
    candidates = [
        page.get_by_role("link", name=re.compile(r"Validate.*Clean\s*Hands", re.I)),
        page.get_by_text(re.compile(r"Validate a Certificate of Clean Hands", re.I), exact=False),
        page.locator("a:has-text('Validate a Certificate of Clean Hands')"),
    ]
    for loc in candidates:
        if await maybe_click(page, loc):
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            return
    raise RuntimeError("Could not find the 'Validate a Certificate of Clean Hands' link.")


async def fill_form_and_search(page: Page, notice: str, last4: str) -> None:
    field_candidates = [
        page.get_by_label(re.compile(r"notice\s*number", re.I)),
        page.get_by_placeholder(re.compile(r"notice", re.I)),
        page.locator("input").nth(0),
    ]
    last4_candidates = [
        page.get_by_label(re.compile(r"(last\s*4|last\s*four)", re.I)),
        page.get_by_placeholder(re.compile(r"last\s*4", re.I)),
        page.locator("input").nth(1),
    ]

    for loc in field_candidates:
        try:
            await loc.fill(notice, timeout=LONG_TIMEOUT)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not fill the Notice Number field.")

    for loc in last4_candidates:
        try:
            await loc.click(timeout=LONG_TIMEOUT)
            await loc.fill(last4, timeout=LONG_TIMEOUT)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not fill the Last 4 field.")

    if not await maybe_click(page, page.get_by_role("button", name=re.compile(r"^Search$", re.I))):
        if not await maybe_click(page, page.locator('button:has-text("Search"), input[type="submit"][value*="Search" i]')):
            await last4_candidates[-1].press("Enter")


async def human_like_delay() -> None:
    """Add random delays to mimic human behavior"""
    import random
    delay = random.uniform(0.5, 2.0)
    await asyncio.sleep(delay)


async def request_current_certificate(page: Page) -> None:
    """Handle both compliant (certificate) and non-compliant (notice) cases"""
    
    # Look for both certificate and notice request links with comprehensive patterns
    req_link_candidates = [
        # Specific certificate patterns
        page.get_by_role("link", name=re.compile(r"request.*Certificate of Clean Hands", re.I)),
        page.get_by_text(re.compile(r"Click here to request a current Certificate of Clean Hands", re.I), exact=False),
        # Specific notice patterns
        page.get_by_role("link", name=re.compile(r"request.*Notice of Non-Compliance", re.I)),
        page.get_by_text(re.compile(r"Click here to request a Notice of Non-Compliance", re.I), exact=False),
        # More variations
        page.locator("a:has-text('request a Notice')"),
        page.locator("a:has-text('request a current')"),
        page.locator("a:has-text('Click here to request')"),
        page.locator("a:has-text('request')"),
        page.locator("a[href*='request']"),
        page.locator("a[href*='Request']"),
        # CSS selectors for any clickable elements with "request" text
        page.locator("*:has-text('Click here to request') >> visible=true"),
    ]
    
    # First, let's see what links are actually available
    try:
        all_links = await page.locator("a").all()
        logger.info(f"Found {len(all_links)} total links on page")
        for i, link in enumerate(all_links[:10]):  # Log first 10 links
            try:
                text = await link.text_content()
                href = await link.get_attribute("href")
                if text and ("request" in text.lower() or "click" in text.lower()):
                    logger.info(f"Link {i}: '{text}' -> {href}")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Could not enumerate links: {e}")
    
    clicked_request = False
    for loc in req_link_candidates:
        if await maybe_click(page, loc):
            logger.info(f"Clicked request link")
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            clicked_request = True
            break
    
    if not clicked_request:
        logger.info("No request link found; continuing.")
        return

    # Add human-like delay before next action
    await human_like_delay()
    
    # Click Next button (verify information step)
    next_clicked = False
    next_candidates = [
        page.get_by_role("button", name=re.compile(r"next", re.I)),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value*='Next' i]"),
        page.locator("button[value*='Next' i]"),
        page.locator("*:has-text('Next') >> visible=true"),
        # Look for any button-like element
        page.locator("button").filter(has_text=re.compile(r"next", re.I)),
    ]
    
    # Debug: let's see what buttons are available
    try:
        all_buttons = await page.locator("button, input[type='submit'], input[type='button']").all()
        logger.info(f"Found {len(all_buttons)} buttons/inputs on page")
        for i, btn in enumerate(all_buttons):
            try:
                text = await btn.text_content()
                value = await btn.get_attribute("value") 
                btn_type = await btn.get_attribute("type")
                logger.info(f"Button {i}: text='{text}' value='{value}' type='{btn_type}'")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Could not enumerate buttons: {e}")
    
    for loc in next_candidates:
        if await maybe_click(page, loc):
            logger.info("Clicked Next button")
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            next_clicked = True
            break
    
    if not next_clicked:
        logger.warning("Next button not found - skipping to submit")
        # Don't return, continue to try submit
    
    # Add human-like delay before submit
    await human_like_delay()
    
    # Click Submit button (confirm submission step)
    submit_clicked = False
    submit_candidates = [
        page.get_by_role("button", name=re.compile(r"submit", re.I)),
        page.locator("button:has-text('Submit')"),
        page.locator("input[type='submit'][value*='Submit' i]"),
        page.locator("button[value*='Submit' i]"),
        page.locator("*:has-text('Submit') >> visible=true"),
        page.locator("button").filter(has_text=re.compile(r"submit", re.I)),
        # Try any submit-type input as fallback
        page.locator("input[type='submit']"),
    ]
    
    for loc in submit_candidates:
        if await maybe_click(page, loc):
            logger.info("Clicked Submit button")
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            submit_clicked = True
            break
    
    if not submit_clicked:
        logger.warning("Submit button not found")
        return
    
    # Wait for confirmation page to load
    await page.wait_for_timeout(2000)
    logger.info("Request submission completed")


async def fetch_certificate_pdf(page: Page, out_path: Path, context: BrowserContext) -> Optional[str]:
    """
    Enhanced PDF fetch with multiple strategies and new tab handling:
      1) expect_download (attachment)
      2) expect_response bytes (inline)
      3) popup -> fetch(window.location.href) (inline in new tab)
      4) context request (authenticated download)
    """
    await human_like_delay()
    
    # Look for View buttons more comprehensively
    candidates = [
        page.get_by_role("button", name=re.compile(r"view\s*(certificate|notice)", re.I)),
        page.get_by_role("link", name=re.compile(r"view\s*(certificate|notice)", re.I)),
        page.get_by_text(re.compile(r"view\s*(certificate|notice)", re.I), exact=False),
        page.locator("button:has-text('View Certificate')"),
        page.locator("button:has-text('View Notice')"),
        page.locator("a:has-text('View Certificate')"),
        page.locator("a:has-text('View Notice')"),
        page.locator("[onclick*='view'], [onclick*='View']"),
    ]

    link = None
    for loc in candidates:
        try:
            if await loc.count() > 0:
                link = loc.first
                logger.info(f"Found view link/button: {await loc.first.text_content()}")
                break
        except Exception:
            continue
    
    if link is None:
        logger.warning("No View Certificate/Notice button found")
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Strategy 1: native download (rare for government sites)
    try:
        logger.info("Trying Strategy 1: Native download")
        async with page.expect_download(timeout=LONG_TIMEOUT) as dl_info:
            await link.click()
        download = await dl_info.value
        await download.save_as(str(out_path))
        logger.info(f"✅ PDF downloaded via native download: {out_path}")
        return str(out_path)
    except Exception as e:
        logger.debug(f"Strategy 1 failed: {e}")

    # Strategy 2: same-tab response bytes
    try:
        logger.info("Trying Strategy 2: Same-tab response")
        async with page.expect_response(lambda r: is_pdf_like_headers(r.headers.get("content-type"), r.url), timeout=LONG_TIMEOUT) as resp_info:
            await link.click()
        resp = await resp_info.value
        data = await resp.body()
        with open(out_path, "wb") as f:
            f.write(data)
        logger.info(f"✅ PDF saved via same-tab response: {out_path} ({len(data)} bytes)")
        return str(out_path)
    except Exception as e:
        logger.debug(f"Strategy 2 failed: {e}")

    # Strategy 3: popup/new tab -> fetch (most common for DC MyTax)
    try:
        logger.info("Trying Strategy 3: Popup/new tab")
        async with page.expect_popup(timeout=LONG_TIMEOUT) as pop_info:
            await link.click()
        popup = await pop_info.value
        logger.info(f"New tab opened: {popup.url}")
        
        # Wait for PDF to load in new tab
        await popup.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        await popup.wait_for_timeout(2000)  # Extra time for PDF viewer
        
        # Check if we can download directly via context request
        if looks_like_pdf_url(popup.url):
            logger.info(f"PDF URL detected in popup: {popup.url}")
            result = await download_via_context_request(context, popup.url, out_path)
            if result:
                await popup.close()
                return result
        
        # Fallback: fetch via JavaScript in the popup
        bytes_list = await popup.evaluate(
            """
            async () => {
                try {
                    const res = await fetch(window.location.href, { credentials: 'include' });
                    if (!res.ok) throw new Error('fetch failed ' + res.status);
                    const ab  = await res.arrayBuffer();
                    return Array.from(new Uint8Array(ab));
                } catch (e) {
                    console.error('Fetch error:', e);
                    return null;
                }
            }
        """
        )
        
        if bytes_list and len(bytes_list) > 0:
            with open(out_path, "wb") as f:
                f.write(bytes(bytes_list))
            logger.info(f"✅ PDF saved via popup fetch: {out_path} ({len(bytes_list)} bytes)")
            await popup.close()
            return str(out_path)
        else:
            logger.warning("Popup fetch returned no data")
            await popup.close()
            
    except Exception as e:
        logger.debug(f"Strategy 3 failed: {e}")

    logger.warning("All PDF fetch strategies failed")
    return None


# ---------------------------
# Main deterministic workflow
# ---------------------------
async def run_workflow(notice: str, last4: str, headless: bool, screenshots: bool, model_name: str) -> WorkflowResult:
    # model_name is unused here (deterministic script), kept to match your CLI
    ts = int(time.time())
    urls: List[str] = []
    result = WorkflowResult(
        status="unknown",
        message="",
        screenshot_path=None,
        pdf_path=None,
        urls=urls,
        notice=notice,
        last4=last4,
    )

    async with async_playwright() as pw:
        # Detect environment and configure browser accordingly
        browser_args = []
        executable_path = None
        
        # Check for Heroku environment
        if os.getenv("DYNO") or os.path.exists("/app"):
            # Heroku environment - use Chrome for Testing buildpack
            if os.path.exists("/app/.chrome-for-testing/chrome-linux64/chrome"):
                executable_path = "/app/.chrome-for-testing/chrome-linux64/chrome"
            # Heroku-specific args for sandboxing
            browser_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"
            ]
        
        # Launch browser with appropriate configuration
        if executable_path:
            logger.info(f"Using Chrome at: {executable_path}")
            browser: Browser = await pw.chromium.launch(
                headless=headless,
                executable_path=executable_path,
                args=browser_args
            )
        else:
            logger.info("Using default Playwright Chromium")
            browser: Browser = await pw.chromium.launch(
                headless=headless,
                args=browser_args if browser_args else None
            )
        context: BrowserContext = await browser.new_context(accept_downloads=True)
        page: Page = await context.new_page()

        # Route-based PDF capture (strongest method)
        out_pdf = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"
        route_state = {"saved": False, "path": None}
        await attach_pdf_route_capture(context, out_pdf, route_state)

        # Track all pages (for harvest)
        known_pages: List[Page] = [page]
        context.on("page", lambda p: known_pages.append(p))

        # 1) Open site
        logger.info("Navigating to https://mytax.dc.gov/_/")
        await page.goto("https://mytax.dc.gov/_/", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        urls.append(page.url)

        # 2) Handle duplicated tab/window security warning if present
        await handle_security_warning(page)
        urls.append(page.url)

        # Screenshots optional
        if screenshots:
            result.screenshot_path = str(ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}-landing.png")
            await page.screenshot(path=result.screenshot_path, full_page=True)

        # 3) Click Validate link
        logger.info("Clicking Validate link")
        await click_validate_link(page)
        urls.append(page.url)

        # 4) Fill form and search
        logger.info("Filling form and searching")
        await fill_form_and_search(page, notice, last4)

        # 5) Classify compliance - wait longer for results to fully load
        await page.wait_for_timeout(3000)  # Wait longer for page to fully load
        try:
            body_text = await page.text_content("body", timeout=NAV_TIMEOUT)
        except Exception:
            body_text = None
        
        logger.info(f"Page text snippet: {(body_text or '')[:200]}...")
        
        # Key insight: Determine compliance status based on what certificate is offered
        # Use regex to allow minor wording variations (e.g., suffixes like "for this taxpayer")
        if re.search(r"click\s*here\s*to\s*request\s*a\s*current\s*certificate\s*of\s*clean\s*hands", (body_text or ""), re.I):
            logger.info("Status: COMPLIANT (offers to request current certificate)")
            status = "compliant"
        elif re.search(r"click\s*here\s*to\s*request.*notice\s*of\s*non[-\s]?compliance", (body_text or ""), re.I):
            logger.info("Status: COMPLIANT (offers to request non-compliance notice - means currently compliant)")
            status = "compliant"
        else:
            status = detect_status_from_text(body_text or "")
            logger.info(f"Status detected from text analysis: {status}")
        
        result.status = status
        result.message = "Detected compliance status from page." if status != "unknown" else "Could not detect compliance status."

        if screenshots:
            shot2 = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}-result.png"
            await page.screenshot(path=str(shot2), full_page=True)

        # 6) Request a current certificate (non-fatal if flow differs)
        logger.info("Attempting to request current certificate (non-fatal if unavailable)")
        try:
            await page.wait_for_timeout(2000)  # Give more time for request links to appear
            await request_current_certificate(page)
            urls.append(page.url)
        except Exception as e:
            logger.info(f"Request flow not completed (non-fatal): {e}")

        # 7) Attempt to fetch via link strategies (must be after request completion)
        logger.info("Attempting to fetch certificate PDF (if available)")
        try:
            got = await fetch_certificate_pdf(page, out_pdf, context)
            if got:
                route_state["saved"] = True
                route_state["path"] = got
        except Exception as e:
            logger.info(f"PDF retrieval via links failed (non-fatal): {e}")

        # 8) If current page IS the PDF, download it via context
        if not route_state["saved"] and looks_like_pdf_url(page.url):
            got = await download_via_context_request(context, page.url, out_pdf)
            if got:
                route_state["saved"] = True
                route_state["path"] = got

        # 9) Harvest any open PDF tabs (viewer already open)
        if not route_state["saved"]:
            await harvest_from_pages(known_pages, out_pdf, route_state)

        # 10) If still not saved but we visited a /Retrieve/ URL, go to it to trigger route capture
        if not route_state["saved"]:
            pdf_url = pick_pdf_url_from_history(urls, page.url)
            if pdf_url:
                logger.info(f"[force] Navigating directly to PDF URL to trigger capture: {pdf_url}")
                try:
                    await page.goto(pdf_url, wait_until="load", timeout=LONG_TIMEOUT)
                    await page.wait_for_timeout(1000)  # allow route to persist
                except Exception as e:
                    logger.debug(f"[force] navigation to pdf failed: {e}")

                if not route_state["saved"]:
                    # Context request then anchor/blob fallbacks
                    got = await download_via_context_request(context, pdf_url, out_pdf)
                    if not got:
                        got = await force_download_via_anchor(page, pdf_url, out_pdf)
                    if not got:
                        got = await force_download_via_blob(page, pdf_url, out_pdf)
                    if got:
                        route_state["saved"] = True
                        route_state["path"] = got

        result.pdf_path = route_state.get("path")
        
        # Final status correction: If we downloaded a PDF, the entity is compliant
        # (The system only allows downloading certificates for compliant entities)
        if result.pdf_path and Path(result.pdf_path).exists():
            logger.info(f"PDF successfully downloaded: {result.pdf_path} - Status corrected to COMPLIANT")
            result.status = "compliant" 
            result.message = "Status confirmed: COMPLIANT (certificate downloaded successfully)"

        await context.close()
        await browser.close()

    return result


# ---------------------------
# CLI + Main
# ---------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic DC MyTax 'Clean Hands' workflow (no LLM)")
    p.add_argument("--notice", default=os.getenv("NOTICE", "L0012322733"), help="Notice Number")
    p.add_argument("--last4", default=os.getenv("L4", "3283"), help="Last 4 digits")
    p.add_argument("--model", default=os.getenv("MODEL_NAME", "gpt-4.1-mini"), help="(unused) kept for parity")
    p.add_argument("--headless", action="store_true", help="Run browser headless")
    p.add_argument("--no-screenshots", dest="screenshots", action="store_false", help="Disable screenshots")
    p.set_defaults(screenshots=True)
    return p.parse_args()


async def main():
    load_dotenv()
    args = parse_args()
    res = await run_workflow(
        notice=args.notice,
        last4=args.last4,
        headless=args.headless,
        screenshots=args.screenshots,
        model_name=args.model,
    )
    print("\n-- Run complete --")
    print("Visited URLs:", res.urls)
    print("Status:", res.status)
    print("PDF path:", res.pdf_path)
    print("Result JSON:\n", res.to_json())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
