"""
gf_minimal_cookie_html.py

Script minimale per:
  - aprire una URL Google Flights con Playwright;
  - superare il consenso cookies, se compare;
  - salvare subito i cookies in JSON;
  - salvare subito l'HTML della pagina corrente.

Uso da Jupyter:
    from gf_minimal_cookie_html import save_google_flights_landing

    url = "https://www.google.com/travel/flights?gl=IT&hl=it&q=Flights%20to%20PMO%20from%20FCO%20on%202026-08-09%20oneway%20economy%20nonstops"

    result = await save_google_flights_landing(
        url,
        headless=False,
        out_dir="gf_landing_artifacts",
    )

    result
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


async def _click_visible(locator, timeout: int = 1500) -> bool:
    try:
        n = await locator.count()
    except Exception:
        n = 0

    for i in range(min(n, 8)):
        item = locator.nth(i)
        try:
            await item.wait_for(state="visible", timeout=timeout)
            await item.click(timeout=timeout)
            return True
        except Exception:
            pass

    return False


async def no_quest(page, timeout: int = 5000) -> bool:
    """
    Supera il consenso Google, se presente.

    Preferisce "Rifiuta tutto" / "Reject all"; se non lo trova,
    prova "Accetta tutto" / "Accept all".
    """
    reject_rx = re.compile(r"^(Rifiuta tutto|Reject all)$", re.I)
    accept_rx = re.compile(r"^(Accetta tutto|Accept all)$", re.I)

    for fr in page.frames:
        locators = (
            fr.get_by_role("button", name=reject_rx),
            fr.locator('button:has-text("Rifiuta tutto")'),
            fr.locator('button:has-text("Reject all")'),
            fr.get_by_role("button", name=accept_rx),
            fr.locator('button:has-text("Accetta tutto")'),
            fr.locator('button:has-text("Accept all")'),
        )

        for loc in locators:
            try:
                if await _click_visible(loc, timeout=timeout):
                    await page.wait_for_timeout(800)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                    except PWTimeout:
                        pass
                    return True
            except Exception:
                pass

    return False


def browser_context_kwargs() -> Dict[str, Any]:
    """
    Contesto simile a quello usato nello scraper principale:
    locale italiana, timezone italiana e user agent desktop.
    """
    return {
        "locale": "it-IT",
        "timezone_id": "Europe/Rome",
        "viewport": {"width": 1365, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }


async def save_google_flights_landing(
    url: str,
    *,
    headless: bool = False,
    out_dir: str | Path = "gf_landing_artifacts",
    cookies_path: Optional[str | Path] = None,
    html_path: Optional[str | Path] = None,
    timeout_ms: int = 45000,
    debug: bool = True,
) -> Dict[str, Any]:
    """
    Apre la URL, supera il consenso cookies, poi salva immediatamente:
      - cookies JSON Playwright;
      - HTML della pagina corrente.

    Ritorna un dizionario con paths, final_url e flag cookie_banner_clicked.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = _ts()
    cookies_path = Path(cookies_path) if cookies_path else out_dir / f"{ts}_cookies.json"
    html_path = Path(html_path) if html_path else out_dir / f"{ts}_landing.html"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(**browser_context_kwargs())
        page = await context.new_page()

        if debug:
            print(f"goto: {url}")

        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        cookie_banner_clicked = await no_quest(page, timeout=7000)

        if debug:
            print(f"cookie banner clicked: {cookie_banner_clicked}")
            print(f"final url before save: {page.url}")

        # Salvataggio immediato dei cookies dopo il consenso.
        cookies = await context.cookies()
        cookies_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Salvataggio immediato dell'HTML della pagina corrente.
        html = await page.content()
        html_path.write_text(html, encoding="utf-8", errors="replace")

        result = {
            "input_url": url,
            "final_url": page.url,
            "cookie_banner_clicked": cookie_banner_clicked,
            "cookies_path": str(cookies_path),
            "cookies_count": len(cookies),
            "html_path": str(html_path),
            "html_bytes": html_path.stat().st_size,
        }

        if debug:
            print(json.dumps(result, ensure_ascii=False, indent=2))

        await context.close()
        await browser.close()

    return result
