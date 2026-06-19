from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


DEFAULT_COOKIES_DIR = "gf_landing_artifacts"
DEFAULT_COOKIES_GLOB = "*_cookies.json"


def find_latest_cookies_path(
    cookies_dir: str | Path = DEFAULT_COOKIES_DIR,
    *,
    cookies_glob: str = DEFAULT_COOKIES_GLOB,
) -> Path:
    """Trova il file cookies JSON più recente salvato da ``gf_ck_gen``.

    Per default cerca in ``gf_landing_artifacts`` i file del tipo
    ``*_cookies.json``, cioè il formato prodotto da ``save_google_flights_landing``.
    La selezione è fatta per data di modifica del file; in caso di parità usa
    il nome file come criterio secondario.
    """
    cookies_dir = Path(cookies_dir)
    if not cookies_dir.exists():
        raise FileNotFoundError(
            f"Directory cookies non trovata: {cookies_dir!s}. "
            "Esegui prima gf_ck_gen.save_google_flights_landing(...)."
        )

    candidates = [p for p in cookies_dir.glob(cookies_glob) if p.is_file()]
    if not candidates:
        raise FileNotFoundError(
            f"Nessun file cookies trovato in {cookies_dir!s} con pattern {cookies_glob!r}. "
            "Esegui prima gf_ck_gen.save_google_flights_landing(...)."
        )

    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def load_playwright_cookies_as_requests_jar(
    cookies_path: str | Path,
) -> requests.cookies.RequestsCookieJar:
    """Converte il JSON cookies prodotto da Playwright in un jar per requests."""
    cookies_path = Path(cookies_path)
    cookies = json.loads(cookies_path.read_text(encoding="utf-8"))

    jar = requests.cookies.RequestsCookieJar()

    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue

        domain = c.get("domain")
        path = c.get("path") or "/"

        if domain:
            jar.set(name, value, domain=domain, path=path)
        else:
            jar.set(name, value, path=path)

    return jar


def requests_get_with_playwright_cookies(
    url: str,
    *,
    cookies_path: Optional[str | Path] = None,
    cookies_dir: str | Path = DEFAULT_COOKIES_DIR,
    cookies_glob: str = DEFAULT_COOKIES_GLOB,
    out_html_path: Optional[str | Path] = "requests_get_response.html",
    timeout: int = 30,
    include_html: bool = True,
) -> Dict[str, Any]:
    """Esegue ``requests.get(url)`` usando cookies Playwright già salvati.

    Novità V2:
      - se ``cookies_path`` non viene passato, usa automaticamente l'ultimo
        file cookies salvato in ``cookies_dir``;
      - il dizionario di output contiene direttamente ``html`` e
        ``response_text`` con ``response.text``, quindi il parser può lavorare
        senza rileggere l'HTML da disco.

    Args:
        url: URL Google Flights da richiedere.
        cookies_path: path esplicito ai cookies. Se omesso, viene cercato
            automaticamente l'ultimo file cookies disponibile.
        cookies_dir: directory in cui cercare i cookies quando ``cookies_path``
            è omesso.
        cookies_glob: pattern dei file cookies da cercare.
        out_html_path: path opzionale in cui salvare anche l'HTML per audit.
            Passare ``None`` per non salvare file.
        timeout: timeout della richiesta requests, in secondi.
        include_html: se ``True``, include ``response.text`` nell'output.

    Returns:
        Un dizionario ``out`` con metadati della request e, se richiesto,
        ``out["html"]`` / ``out["response_text"]``.
    """
    resolved_cookies_path = Path(cookies_path) if cookies_path else find_latest_cookies_path(
        cookies_dir,
        cookies_glob=cookies_glob,
    )

    jar = load_playwright_cookies_as_requests_jar(resolved_cookies_path)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
    }

    response = requests.get(
        url,
        headers=headers,
        cookies=jar,
        timeout=timeout,
        allow_redirects=True,
    )

    html = response.text

    saved_path = None
    if out_html_path is not None:
        out_html_path = Path(out_html_path)
        out_html_path.parent.mkdir(parents=True, exist_ok=True)
        out_html_path.write_text(
            html,
            encoding=response.encoding or "utf-8",
            errors="replace",
        )
        saved_path = str(out_html_path)

    out: Dict[str, Any] = {
        "status_code": response.status_code,
        "ok": response.ok,
        "final_url": response.url,
        "cookies_path": str(resolved_cookies_path),
        "cookies_source": "explicit" if cookies_path else "latest_saved",
        "saved_path": saved_path,
        "response_bytes": len(response.content),
        "html_bytes": len(html.encode("utf-8", errors="replace")),
        "content_type": response.headers.get("content-type"),
        "encoding": response.encoding,
    }

    if include_html:
        out["html"] = html
        out["response_text"] = html

    return out
