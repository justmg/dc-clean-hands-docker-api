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

from browser_use import (
    Agent,
    Controller,
    ActionResult,
    Browser,
    BrowserConfig,
    BrowserContextConfig,
)
from langchain_openai import ChatOpenAI
from playwright.async_api import Page

# ---------------------------
# Configuration & Logging
# ---------------------------
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

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


controller = Controller()

# ---------------------------
# PDF Detection & Utilities
# ---------------------------
def _looks_like_pdf_url(url: str) -> bool:
    u = (url or "").lower()
    return u.endswith(".pdf") or ("/retrieve/" in u and "file__=" in u)


def _is_pdf_like_headers(ct: Optional[str], url: Optional[str]) -> bool:
    ct = (ct or "").lower()
    u = (url or "").lower()
    return (
        "application/pdf" in ct
        or "application/octet-stream" in ct
        or "application/force-download" in ct
        or _looks_like_pdf_url(u)
    )


def _is_pdf_response(resp) -> bool:
    try:
        return _is_pdf_like_headers(resp.headers.get("content-type"), getattr(resp, "url", ""))
    except Exception:
        return False


async def _save_resp_pdf(resp, out_path: Path, flag: dict):
    """Save response bytes once (guard against races)."""
    if flag.get("saved"):
        return
    try:
        data = await resp.body()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        flag["saved"] = True
        flag["path"] = str(out_path)
        logger.info(f"[sniffer] Saved PDF from {resp.url} -> {out_path}")
    except Exception as e:
        logger.warning(f"[sniffer] Failed to save PDF: {e}")


async def harvest_from_pages(pages: List[Page], out_path: Path, state: dict):
    """Post-run harvest: if any known page shows a PDF URL, fetch bytes via page context."""
    if state.get("saved"):
        return state["path"]
    for p in pages:
        try:
            if _looks_like_pdf_url(getattr(p, "url", "")):
                logger.info(f"[harvest] Found open PDF tab: {p.url}")
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


async def force_download_via_anchor(page: Page, url: str, out_path: Path) -> Optional[str]:
    """Force the browser to download same-origin PDF using <a download>."""
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
    """Fetch to blob, convert to object URL, trigger download; preserves session cookies."""
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


def pick_pdf_url_from_history(history_urls: List[str], current_url: Optional[str]) -> Optional[str]:
    """Pick the best /Retrieve/... URL from the agent's history or current page."""
    for u in reversed(history_urls or []):
        if _looks_like_pdf_url(u):
            return u
    if current_url and _looks_like_pdf_url(current_url):
        return current_url
    return None


async def download_via_context_request(page: Page, url: str, out_path: Path) -> Optional[str]:
    """
    Use Playwright's APIRequestContext (page.context.request) to GET the PDF with
    the same cookies/session as the page, then write bytes to disk.
    """
    logger.info(f"[context-request] Starting download from: {url}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = await page.context.request.get(
            url,
            headers={
                "Accept": "application/pdf,application/octet-stream,*/*",
                "Referer": "https://mytax.dc.gov/_/",
            },
            timeout=LONG_TIMEOUT / 1000,  # seconds
        )
        logger.info(f"[context-request] Response status: {resp.status}")
        if not resp.ok:
            logger.warning(f"[context-request] GET {url} failed: {resp.status}")
            return None
        data = await resp.body()
        logger.info(f"[context-request] Got {len(data)} bytes")
        with open(out_path, "wb") as f:
            f.write(data)
        logger.info(f"[context-request] Saved -> {out_path}")
        return str(out_path)
    except Exception as e:
        logger.warning(f"[context-request] error: {e}")
        return None

# ---------------------------
# Route-based capture (bulletproof)
# ---------------------------
async def attach_pdf_route_capture(page: Page, out_path: Path, state: dict):
    """
    Intercept any request that looks like the PDF and persist bytes via route.fetch().
    Works for inline view, streaming, and attachments; also propagates to popups.
    """
    if getattr(page, "_pdf_route_attached", False):
        return
    setattr(page, "_pdf_route_attached", True)

    async def handler(route, request):
        url = request.url or ""
        if state.get("saved"):
            await route.continue_()
            return
        if _looks_like_pdf_url(url):
            try:
                response = await route.fetch()
                body = await response.body()
                # Persist
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(body)
                state["saved"] = True
                state["path"] = str(out_path)
                logger.info(f"[route] Saved PDF via route: {out_path} ({len(body)} bytes) from {url}")
                # Fulfill to let the browser still render it if needed
                headers = dict(response.headers)
                await route.fulfill(status=response.status, headers=headers, body=body)
            except Exception as e:
                logger.warning(f"[route] error for {url}: {e}")
                await route.continue_()
        else:
            await route.continue_()

    await page.route("**/*", handler)

    def on_popup(popup: Page):
        # Attach the same route capture to popups as well
        asyncio.create_task(attach_pdf_route_capture(popup, out_path, state))

    page.on("popup", on_popup)
    logger.info(f"[route] Route capture attached to page id={id(page)}")

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
    if re.search(r"\bnon[-\s]?compliant\b", text, re.I):
        return "noncompliant"
    if re.search(r"\bcompliant\b", text, re.I):
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


async def request_current_certificate(page: Page) -> None:
    req_link_candidates = [
        page.get_by_role("link", name=re.compile(r"request.*Certificate of Clean Hands", re.I)),
        page.get_by_text(re.compile(r"Click here to request a current Certificate of Clean Hands", re.I), exact=False),
    ]
    for loc in req_link_candidates:
        if await maybe_click(page, loc):
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
            break
    else:
        logger.info("Request link not found; continuing.")
        return

    await page.wait_for_timeout(1000)
    if await maybe_click(page, page.get_by_role("button", name=re.compile(r"^Next$", re.I))):
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(1000)
    if await maybe_click(page, page.get_by_role("button", name=re.compile(r"Submit", re.I))):
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)


async def fetch_certificate_pdf(page: Page, out_path: Path) -> Optional[str]:
    """
    Deterministic fetch from the page:
      1) expect_download (attachment)
      2) expect_response & resp.body() on same tab (inline)
      3) popup -> fetch(window.location.href) (inline)
    """
    candidates = [
        page.get_by_role("link", name=re.compile(r"view\s*(certificate|notice)", re.I)),
        page.get_by_text(re.compile(r"view\s*(certificate|notice)", re.I), exact=False),
        page.locator("a:has-text('View Certificate')"),
        page.locator("a:has-text('View Notice')"),
    ]

    link = None
    for loc in candidates:
        try:
            if await loc.count() > 0:
                link = loc.first
                break
        except Exception:
            continue
    if link is None:
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Strategy 1: native download
    try:
        async with page.expect_download(timeout=LONG_TIMEOUT) as dl_info:
            await link.click()
        download = await dl_info.value
        await download.save_as(str(out_path))
        logger.info(f"PDF downloaded via native download: {out_path}")
        return str(out_path)
    except Exception:
        pass

    # Strategy 2: same-tab response bytes
    try:
        async with page.expect_response(lambda r: _is_pdf_response(r), timeout=LONG_TIMEOUT) as resp_info:
            await link.click()
        resp = await resp_info.value
        logger.info(f"PDF response: {resp.url} (content-type: {resp.headers.get('content-type')})")
        data = await resp.body()
        with open(out_path, "wb") as f:
            f.write(data)
        logger.info(f"PDF saved via same-tab response: {out_path}")
        return str(out_path)
    except Exception:
        pass

    # Strategy 3: popup page -> fetch
    try:
        async with page.expect_popup() as pop_info:
            await link.click()
        popup = await pop_info.value
        await popup.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        bytes_list = await popup.evaluate(
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
            with open(out_path, "wb") as f:
                f.write(bytes(bytes_list))
            logger.info(f"PDF saved via popup fetch: {out_path}")
            return str(out_path)
    except Exception:
        pass

    return None

# ---------------------------
# Deterministic Action
# ---------------------------
@controller.action("Run DC Clean Hands workflow deterministically")
async def clean_hands_workflow(
    notice: str,
    last4: str,
    browser: Browser,
    screenshots: bool = False,
    ts: Optional[int] = None,
) -> ActionResult:
    ts = ts or int(time.time())
    urls: List[str] = []
    pdf_path: Optional[str] = None
    screenshot_path: Optional[str] = None

    result = WorkflowResult(
        status="unknown",
        message="",
        screenshot_path=None,
        pdf_path=None,
        urls=urls,
        notice=notice,
        last4=last4,
    )

    page = await browser.get_current_page()

    # 1) Open site
    logger.info("Navigating to mytax.dc.gov")
    await page.goto("https://mytax.dc.gov/_/", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
    urls.append(page.url)

    # 2) Handle duplicated tab/window warning if present
    await handle_security_warning(page)
    urls.append(page.url)

    # Optional screenshot
    screenshot_path = await save_screenshot(
        page, ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}-landing.png", enable=screenshots
    )

    # 3) Click Validate link
    logger.info("Clicking Validate link")
    try:
        await click_validate_link(page)
        urls.append(page.url)
    except Exception as e:
        msg = f"Failed to find/click 'Validate a Certificate of Clean Hands': {type(e).__name__}: {e}"
        logger.error(msg)
        return ActionResult(error=msg)

    # 4) Fill form and search
    logger.info("Filling form and searching")
    try:
        await fill_form_and_search(page, notice, last4)
    except Exception as e:
        msg = f"Failed to fill form: {type(e).__name__}: {e}"
        logger.error(msg)
        return ActionResult(error=msg)

    # 5) Classify compliance
    await page.wait_for_timeout(1500)
    try:
        body_text = await page.text_content("body", timeout=NAV_TIMEOUT)
    except Exception:
        body_text = None

    status = detect_status_from_text(body_text or "")
    result.status = status
    result.message = "Detected compliance status from page." if status != "unknown" else "Could not detect compliance status."

    # Optional screenshot
    screenshot_path = await save_screenshot(
        page, ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}-result.png", enable=screenshots
    )

    # 6) Request a current certificate (non-fatal if flow differs)
    logger.info("Attempting to request current certificate (non-fatal if unavailable)")
    try:
        await request_current_certificate(page)
        urls.append(page.url)
    except Exception as e:
        logger.info(f"Request flow not completed (non-fatal): {e}")

    # 7) Attempt to fetch PDF via link strategies (may or may not be present here)
    logger.info("Attempting to fetch certificate PDF (if available)")
    out_pdf = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"
    try:
        got_pdf = await fetch_certificate_pdf(page, out_pdf)
        if got_pdf:
            pdf_path = got_pdf
            result.pdf_path = pdf_path
    except Exception as e:
        logger.info(f"PDF retrieval failed (non-fatal): {e}")

    # 8) If we're on a PDF page (Retrieve URL), download it directly via browser context
    if not pdf_path and _looks_like_pdf_url(page.url):
        logger.info(f"Currently on PDF page: {page.url}, attempting direct download")
        try:
            got_pdf = await download_via_context_request(page, page.url, out_pdf)
            if got_pdf:
                pdf_path = got_pdf
                result.pdf_path = pdf_path
                logger.info(f"Successfully downloaded PDF from current page: {got_pdf}")
        except Exception as e:
            logger.info(f"Direct download from current page failed: {e}")

    result.screenshot_path = screenshot_path
    return ActionResult(extracted_content=result.to_json(), is_done=True)

# ---------------------------
# Entrypoint
# ---------------------------
async def run_agent(
    notice: str,
    last4: str,
    model_name: str,
    headless: bool,
    screenshots: bool,
):
    llm = ChatOpenAI(model=model_name)

    context_config = BrowserContextConfig(
        save_downloads_path=str(ARTIFACTS_DIR),
    )
    browser_config = BrowserConfig(headless=headless, new_context_config=context_config)
    browser = Browser(config=browser_config)

    # Single timestamp for the whole run
    ts = int(time.time())
    sniff_out = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"
    sniff_state = {"saved": False, "path": None}

    # Attach route-based capture + response sniffer to initial page (and to popups)
    known_pages: List[Page] = []

    def track_page(page: Page):
        if page in known_pages:
            return
        known_pages.append(page)

    try:
        initial_page = await browser.get_current_page()
        track_page(initial_page)
        await attach_pdf_route_capture(initial_page, sniff_out, sniff_state)

        # Also attach a lightweight response sniffer (secondary signal)
        def handle_response(resp):
            if sniff_state.get("saved"):
                return
            try:
                ct = (resp.headers.get("content-type") or "").lower()
            except Exception:
                ct = ""
            url = getattr(resp, "url", "")
            if _is_pdf_like_headers(ct, url):
                asyncio.create_task(_save_resp_pdf(resp, sniff_out, sniff_state))

        def handle_popup(popup: Page):
            track_page(popup)
            asyncio.create_task(attach_pdf_route_capture(popup, sniff_out, sniff_state))
            popup.on("response", handle_response)

        initial_page.on("response", handle_response)
        initial_page.on("popup", handle_popup)
        logger.info(f"[init] Handlers attached to page id={id(initial_page)}")

    except Exception as e:
        logger.debug(f"[init] Failed to attach handlers to initial page: {e}")

    # Initial deterministic action with shared timestamp
    initial_actions = [
        {"clean_hands_workflow": {"notice": notice, "last4": last4, "screenshots": screenshots, "ts": ts}},
    ]

    agent = Agent(
        task=(
            f"Validate DC Clean Hands for notice {notice}. "
            f"If compliant: download Clean Hands Certificate PDF. "
            f"If NOT compliant: request and download Notice of Non-Compliance PDF to artifacts folder. "
            f"Always get the PDF document."
        ),
        llm=llm,
        controller=controller,
        browser=browser,
        initial_actions=initial_actions,
    )

    history = await agent.run(max_steps=200)

    # Harvest any open PDF tabs from known pages (if route didn't already save)
    logger.info("[harvest] Checking for open PDF tabs...")
    await harvest_from_pages(known_pages, sniff_out, sniff_state)

    # Deterministic reattempt via link strategies on current page
    if not sniff_state["saved"]:
        logger.info("[post-run] Attempting deterministic PDF fetch...")
        try:
            current_page = await browser.get_current_page()
            out_pdf = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"
            got_pdf = await fetch_certificate_pdf(current_page, out_pdf)
            if got_pdf:
                logger.info(f"[post-run] PDF saved via deterministic fetch: {got_pdf}")
                sniff_state["saved"] = True
                sniff_state["path"] = got_pdf
        except Exception as e:
            logger.debug(f"[post-run] deterministic fetch failed: {e}")

    # If current page IS the PDF, download it directly (context request)
    if not sniff_state["saved"]:
        try:
            current_page = await browser.get_current_page()
            current_url = getattr(current_page, "url", "")
            if _looks_like_pdf_url(current_url):
                logger.info(f"[post-run] Current page is PDF: {current_url}, downloading directly")
                out_pdf = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"
                got_pdf = await download_via_context_request(current_page, current_url, out_pdf)
                if got_pdf:
                    logger.info(f"[post-run] PDF saved directly from current page: {got_pdf}")
                    sniff_state["saved"] = True
                    sniff_state["path"] = got_pdf
        except Exception as e:
            logger.debug(f"[post-run] direct download from current page failed: {e}")

    # Forced download using exact URL from history — navigate to trigger route capture, then fallbacks
    if not sniff_state["saved"]:
        logger.info("[force] Entering forced download section...")
        try:
            visited = []
            try:
                visited = history.urls()
            except Exception:
                pass

            current_page = await browser.get_current_page()
            current_url = getattr(current_page, "url", None)
            pdf_url = pick_pdf_url_from_history(visited, current_url)

            logger.info(f"[force] Visited URLs: {visited}")
            logger.info(f"[force] Chosen PDF URL: {pdf_url}")

            if pdf_url:
                out_pdf = ARTIFACTS_DIR / f"clean-hands-{notice}-{ts}.pdf"

                # Navigate to the PDF URL to trigger the route capture (strongest method)
                try:
                    logger.info(f"[force] Navigating to PDF URL to trigger route capture: {pdf_url}")
                    await current_page.goto(pdf_url, wait_until="load", timeout=LONG_TIMEOUT)
                    # Small wait to allow route handler to persist file
                    await current_page.wait_for_timeout(1000)
                except Exception as e:
                    logger.debug(f"[force] Navigation to PDF URL failed: {e}")

                # If still not saved, try context request + anchor/blob fallbacks
                if not sniff_state["saved"]:
                    logger.info("[force] Trying context request fallback...")
                    got = await download_via_context_request(current_page, pdf_url, out_pdf)

                    if not got:
                        logger.info("[force] Context request failed; trying anchor fallback")
                        got = await force_download_via_anchor(current_page, pdf_url, out_pdf)
                    if not got:
                        logger.info("[force] Anchor fallback failed; trying blob fallback")
                        got = await force_download_via_blob(current_page, pdf_url, out_pdf)

                    if got:
                        sniff_state["saved"] = True
                        sniff_state["path"] = got
                        logger.info(f"[force] Success -> {got}")
                    else:
                        logger.warning("[force] All forced download methods failed.")
            else:
                logger.warning("[force] No Retrieve URL found to force-download.")
        except Exception as e:
            logger.debug(f"[force] unexpected error: {e}")

    print("\n-- Agent run complete --")
    try:
        print("Visited URLs:", history.urls())
    except Exception:
        pass

    if sniff_state["saved"]:
        print(f"PDF SAVED: {sniff_state['path']}")
    else:
        print("No PDF was captured")

    final = history.final_result()
    if final:
        print("Final Result JSON:\n", final)
    else:
        print("No final result returned.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DC Clean Hands deterministic workflow")
    p.add_argument("--notice", default=os.getenv("NOTICE", "L0012322733"), help="Notice Number")
    p.add_argument("--last4", default=os.getenv("L4", "3283"), help="Last 4 digits")
    p.add_argument("--model", default=os.getenv("MODEL_NAME", "gpt-4.1-mini"), help="LLM model name")
    p.add_argument("--headless", action="store_true", help="Run browser headless")
    p.add_argument("--no-screenshots", dest="screenshots", action="store_false", help="Disable screenshots")
    p.set_defaults(screenshots=True)
    return p.parse_args()


async def main():
    load_dotenv()
    args = parse_args()

    await run_agent(
        notice=args.notice,
        last4=args.last4,
        model_name=args.model,
        headless=args.headless,
        screenshots=args.screenshots,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
