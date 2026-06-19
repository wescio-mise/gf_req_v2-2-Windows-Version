"""
Minimal parser for Google Flights HTML from requests.get(...).text.

The parser is intentionally based on the rendered/server-side HTML flight cards,
not on private API payloads. It looks for Google Flights result cards such as:

    <li class="pIav2d"> ...
      <div class="JMc5Xc" aria-label="A partire da 46 euro. ... Seleziona volo">

Notebook usage:

    from gf_parse_requests_html import parse_google_flights_html

    df, info = parse_google_flights_html(out["html"])
    # oppure: df, info = parse_google_flights_html_text(out["html"])
    display(df)
    info

CLI usage:

    python gf_parse_requests_html.py \
      --html gf_landing_artifacts/requests_get_response.html \
      --out flights.csv \
      --json-out flights.json \
      --summary-out parse_summary.json

Notes:
    - This parser is best-effort. Google may change the HTML/classes/aria-labels.
    - If the HTML is only a consent page, error page, or JS bootstrap with no
      server-rendered flight result cards, the output DataFrame will be empty
      and info["warnings"] will explain what was found.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote_plus, urlparse

import pandas as pd
from bs4 import BeautifulSoup


PRICE_RX = re.compile(r"(?:A partire da|Da)\s+([0-9][0-9.\s]*)\s+euro", re.I)
PRICE_EURO_SYMBOL_RX = re.compile(r"([0-9][0-9.\s]*)\s*€")
TIME_RX = re.compile(r"\b\d{1,2}:\d{2}\b")
IATA_RX = re.compile(r"^[A-Z]{3}$")
EMISSIONS_RX = re.compile(r"([0-9]+(?:[,.][0-9]+)?)\s*kg\s+di\s+CO2e", re.I)

# Main aria-label pattern observed in Italian Google Flights pages.
ITINERARY_LABEL_RX = re.compile(
    r"Partenza da\s+(?P<origin_airport_name>.+?)\s+alle ore\s+"
    r"(?P<depart_time>\d{1,2}:\d{2})\s+il giorno\s+"
    r"(?P<depart_day_text>.+?)\s+e arrivo a\s+"
    r"(?P<destination_airport_name>.+?)\s+alle ore\s+"
    r"(?P<arrive_time>\d{1,2}:\d{2})\s+il giorno\s+"
    r"(?P<arrive_day_text>.+?)\.\s+Durata totale\s+"
    r"(?P<duration_text>.+?)\."
)

CARRIER_RX = re.compile(r"Volo\s+(?P<stops_text>.+?)\s+con\s+(?P<airline>.+?)\.")
OPERATED_BY_RX = re.compile(r"Operato da\s+(?P<operated_by>.+?)\.")
QUERY_TEXT_RX = re.compile(
    r"Flights\s+to\s+(?P<destination>[A-Z]{3})\s+from\s+"
    r"(?P<origin>[A-Z]{3})\s+on\s+(?P<depart_date>\d{4}-\d{2}-\d{2})",
    re.I,
)

# Observed in Google Flights cards inside data-travelimpactmodelwebsiteurl, e.g.:
# https://www.travelimpactmodel.org/lookup/flight?itinerary=FCO-CTA-FR-4929-20260809
# Multiple segments, if present, are handled by finding every matching segment.
TIM_ITINERARY_SEGMENT_RX = re.compile(
    r"(?P<origin>[A-Z]{3})-(?P<destination>[A-Z]{3})-"
    r"(?P<carrier>[A-Z0-9]{2,3})-(?P<number>[A-Z0-9]{1,5})-"
    r"(?P<date>\d{8})"
)


def _norm(text: Any) -> str:
    """Normalize whitespace and HTML entities."""
    if text is None:
        return ""
    return " ".join(html_lib.unescape(str(text)).replace("\xa0", " ").split())


def _int_from_number_text(text: str) -> Optional[int]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9]", "", text)
    return int(cleaned) if cleaned else None


def _float_from_number_text(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = text.replace(".", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_price(label: str, card_text: str) -> Optional[int]:
    for rx in (PRICE_RX, PRICE_EURO_SYMBOL_RX):
        m = rx.search(label) or rx.search(card_text)
        if m:
            return _int_from_number_text(m.group(1))
    return None


def _parse_stops_count(stops_text: str) -> Optional[int]:
    stops = _norm(stops_text).lower()
    if not stops:
        return None
    if "senza scali" in stops or "diretto" in stops or "nonstop" in stops:
        return 0
    # Italian labels usually use numerals, but keep a few common words too.
    m = re.search(r"(\d+)\s+scal", stops)
    if m:
        return int(m.group(1))
    words = {"uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4}
    for word, value in words.items():
        if re.search(rf"\b{word}\b.*\bscal", stops):
            return value
    return None


def _extract_iata_codes_from_card(card) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Extract origin/destination IATA codes from visible card DOM when available."""
    codes: List[str] = []

    # In the observed HTML, each airport block has class QylvBf and contains
    # a visible IATA span, e.g. FCO / CTA.
    for airport_block in card.select(".QylvBf"):
        for span in airport_block.find_all("span"):
            t = _norm(span.get_text(" ", strip=True))
            if IATA_RX.fullmatch(t):
                codes.append(t)
                break

    # Fallback: inspect short spans in the card text.
    if len(codes) < 2:
        seen = set(codes)
        for span in card.find_all("span"):
            t = _norm(span.get_text(" ", strip=True))
            if IATA_RX.fullmatch(t) and t not in seen:
                codes.append(t)
                seen.add(t)
                if len(codes) >= 2:
                    break

    origin = codes[0] if len(codes) >= 1 else None
    destination = codes[1] if len(codes) >= 2 else None
    return origin, destination, codes


def _extract_section(card) -> Optional[str]:
    """Best-effort section name, e.g. 'Voli migliori in partenza' or 'Altri voli'."""
    # Most result groups are enclosed in a div containing an h3 and a ul.
    for ancestor in card.parents:
        if getattr(ancestor, "name", None) in {"div", "section"}:
            h = ancestor.find("h3")
            if h:
                text = _norm(h.get_text(" ", strip=True))
                if text:
                    return text
    prev_h3 = card.find_previous("h3")
    return _norm(prev_h3.get_text(" ", strip=True)) if prev_h3 else None


def _extract_card_id(card) -> Optional[str]:
    node = card.find(attrs={"data-id": True})
    if node and node.get("data-id"):
        return str(node.get("data-id"))
    if card.get("ssk"):
        return str(card.get("ssk"))
    return None


def _date_yyyymmdd_to_iso(text: Optional[str]) -> Optional[str]:
    if not text or not re.fullmatch(r"\d{8}", str(text)):
        return None
    text = str(text)
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _extract_travel_impact_itinerary(card) -> Tuple[Optional[str], Optional[str]]:
    """Return (Travel Impact Model URL, itinerary param) from a flight card, if present."""
    if card is None:
        return None, None
    node = card.find(attrs={"data-travelimpactmodelwebsiteurl": True})
    if not node:
        return None, None

    url = _norm(node.get("data-travelimpactmodelwebsiteurl"))
    if not url:
        return None, None

    itinerary = None
    try:
        parsed = urlparse(url)
        itinerary = parse_qs(parsed.query).get("itinerary", [None])[0]
    except Exception:
        itinerary = None

    # Fallback for malformed/escaped URLs.
    if not itinerary:
        m = re.search(r"(?:^|[?&])itinerary=([^&]+)", url)
        if m:
            itinerary = unquote_plus(m.group(1))

    return url, itinerary


def _parse_travel_impact_itinerary(itinerary: Optional[str]) -> Dict[str, Any]:
    """Parse itinerary strings such as FCO-CTA-FR-4929-20260809.

    For multi-segment itineraries, ``flight_id`` joins segment IDs with '+',
    e.g. AZ123+BA456. For the direct flights observed so far, it is simply
    carrier + number, e.g. FR4929.
    """
    out: Dict[str, Any] = {
        "flight_id": None,
        "flight_id_source": None,
        "carrier_code": None,
        "flight_number": None,
        "flight_departure_date": None,
        "flight_segments_count": None,
        "flight_segment_ids": None,
        "tim_itinerary": itinerary,
    }
    if not itinerary:
        return out

    matches = list(TIM_ITINERARY_SEGMENT_RX.finditer(itinerary))
    if not matches:
        return out

    segments: List[Dict[str, str]] = []
    segment_ids: List[str] = []
    for m in matches:
        carrier = m.group("carrier").upper()
        number = m.group("number").upper()
        segment_id = f"{carrier}{number}"
        segment_ids.append(segment_id)
        segments.append({
            "origin": m.group("origin").upper(),
            "destination": m.group("destination").upper(),
            "carrier": carrier,
            "number": number,
            "date": _date_yyyymmdd_to_iso(m.group("date")) or m.group("date"),
            "flight_id": segment_id,
        })

    out.update({
        "flight_id": "+".join(segment_ids),
        "flight_id_source": "travelimpactmodel_itinerary",
        "carrier_code": segments[0]["carrier"],
        "flight_number": segments[0]["number"],
        "flight_departure_date": segments[0]["date"],
        "flight_segments_count": len(segments),
        "flight_segment_ids": "+".join(segment_ids),
        "flight_segments_json": json.dumps(segments, ensure_ascii=False),
    })
    return out


def _make_fallback_flight_id(row: Dict[str, Any]) -> str:
    """Create a stable fallback ID when no explicit flight number is present.

    This is not an official airline flight number; it is a deterministic hash
    of the parsed card fields, useful for dedupe/audit only.
    """
    fields = [
        row.get("query_depart_date"),
        row.get("origin"),
        row.get("destination"),
        row.get("depart_time"),
        row.get("arrive_time"),
        row.get("airline"),
        row.get("operated_by"),
        row.get("duration_text"),
        row.get("stops_text"),
        row.get("price_eur"),
    ]
    key = "|".join("" if v is None else str(v) for v in fields)
    digest = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"gf_{digest}"


def _parse_flight_label(label: str, card_text: str, card=None, index: int = 0) -> Dict[str, Any]:
    label = _norm(label)
    card_text = _norm(card_text)

    row: Dict[str, Any] = {
        "result_index": index,
        "card_id": _extract_card_id(card) if card is not None else None,
        "section": _extract_section(card) if card is not None else None,
        "flight_id": None,
        "flight_id_source": None,
        "carrier_code": None,
        "flight_number": None,
        "flight_departure_date": None,
        "flight_segments_count": None,
        "flight_segment_ids": None,
        "tim_itinerary": None,
        "travelimpactmodel_url": None,
        "price_eur": _parse_price(label, card_text),
        "currency": "EUR",
        "airline": None,
        "operated_by": None,
        "stops_text": None,
        "stops_count": None,
        "duration_text": None,
        "depart_time": None,
        "arrive_time": None,
        "depart_day_text": None,
        "arrive_day_text": None,
        "origin": None,
        "destination": None,
        "origin_airport_name": None,
        "destination_airport_name": None,
        "emissions_kg_co2e": None,
        "baggage_cabin_not_included": "cappelliera" in label.lower() or "cappelliere" in card_text.lower(),
        "raw_label": label,
    }

    m = CARRIER_RX.search(label)
    if m:
        row["stops_text"] = _norm(m.group("stops_text"))
        row["stops_count"] = _parse_stops_count(row["stops_text"])
        row["airline"] = _norm(m.group("airline"))

    m = OPERATED_BY_RX.search(label)
    if m:
        row["operated_by"] = _norm(m.group("operated_by"))

    m = ITINERARY_LABEL_RX.search(label)
    if m:
        for k, v in m.groupdict().items():
            row[k] = _norm(v)

    if card is not None:
        origin, destination, all_codes = _extract_iata_codes_from_card(card)
        row["origin"] = origin
        row["destination"] = destination
        if all_codes:
            row["airport_codes_found"] = ",".join(all_codes)

    m = EMISSIONS_RX.search(card_text) or EMISSIONS_RX.search(label)
    if m:
        row["emissions_kg_co2e"] = _float_from_number_text(m.group(1))

    if card is not None:
        tim_url, tim_itinerary = _extract_travel_impact_itinerary(card)
        row["travelimpactmodel_url"] = tim_url
        row.update(_parse_travel_impact_itinerary(tim_itinerary))

    return row


def _decode_google_escaped_text(text: str) -> str:
    """Decode the common JS escaping used in Google HTML enough to find URLs/query."""
    replacements = {
        r"\u003d": "=",
        r"\u0026": "&",
        r"\u002b": "+",
        r"\u002B": "+",
        r"\/": "/",
        "&amp;": "&",
    }
    out = text
    for old, new in replacements.items():
        out = out.replace(old, new)
    return html_lib.unescape(out)


def _extract_query_metadata(html_text: str, soup: BeautifulSoup) -> Dict[str, Any]:
    decoded = _decode_google_escaped_text(html_text)

    info: Dict[str, Any] = {
        "title": _norm(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "source_url": None,
        "query_text": None,
        "query_origin": None,
        "query_destination": None,
        "query_depart_date": None,
    }

    # Prefer explicit Google Flights URL embedded in AF_dataServiceRequests or consent prev URL.
    url_match = re.search(r"https://www\.google\.com/travel/flights\?[^\"'<>\s]+", decoded)
    if url_match:
        url = url_match.group(0)
        # Stop at common delimiters that can appear after escaped URLs in JS strings.
        url = re.split(r"[\\]", url)[0]
        # In consent/setprefs wrappers Google often percent-encodes the whole
        # Flights query string. Decode a couple of times before parsing.
        for _ in range(2):
            decoded_url = unquote_plus(url)
            if decoded_url == url:
                break
            url = decoded_url
        info["source_url"] = url
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        q = qs.get("q", [None])[0]
        if q:
            q = unquote_plus(q)
            info["query_text"] = q
            m = QUERY_TEXT_RX.search(q)
            if m:
                info["query_origin"] = m.group("origin").upper()
                info["query_destination"] = m.group("destination").upper()
                info["query_depart_date"] = m.group("depart_date")

    # Fallback: inspect the full HTML for the English query text.
    if not info["query_text"]:
        m = QUERY_TEXT_RX.search(decoded)
        if m:
            info["query_text"] = m.group(0)
            info["query_origin"] = m.group("origin").upper()
            info["query_destination"] = m.group("destination").upper()
            info["query_depart_date"] = m.group("depart_date")

    return info



def _looks_like_html_text(value: Any) -> bool:
    """True quando una stringa sembra contenere HTML, non un path locale."""
    if not isinstance(value, str):
        return False
    sample = value[:5000].lstrip().lower()
    if not sample:
        return False
    return (
        sample.startswith("<!doctype html")
        or sample.startswith("<html")
        or "<html" in sample[:500]
        or "<body" in sample[:1000]
        or "google" in sample[:5000] and "<script" in sample[:5000]
    )


def parse_google_flights_html_text(
    html_text: str,
    *,
    include_raw_label: bool = True,
    dedupe: bool = True,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Parse diretto da ``response.text`` / ``out["html"]`` senza file locale."""
    return parse_google_flights_html(
        html_text=html_text,
        include_raw_label=include_raw_label,
        dedupe=dedupe,
    )

def parse_google_flights_html(
    html_path: str | Path | None = None,
    *,
    html_text: Optional[str] = None,
    include_raw_label: bool = True,
    dedupe: bool = True,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Parse a Google Flights HTML page saved from requests.get(...).text.

    Args:
        html_path: path to the HTML file.
        html_text: alternatively pass the HTML content directly.
        include_raw_label: keep the long aria-label column for audit/debug.
        dedupe: remove duplicated result cards that Google may render twice
            in the same HTML for responsive/interactive layouts.

    Returns:
        (df, info), where df has one row per parsed flight card and info has
        diagnostics and query metadata.
    """
    if html_text is None:
        if html_path is None:
            raise ValueError("Provide either html_path or html_text")
        # V2: accetta anche l'HTML diretto come primo argomento posizionale,
        # così si può chiamare parse_google_flights_html(out["html"], ...).
        if _looks_like_html_text(html_path):
            html_text = str(html_path)
            html_path = None
        else:
            html_path = Path(html_path)
            html_text = html_path.read_text(encoding="utf-8", errors="replace")
    else:
        html_path = Path(html_path) if html_path is not None else None

    soup = BeautifulSoup(html_text, "html.parser")
    query_info = _extract_query_metadata(html_text, soup)

    rows: List[Dict[str, Any]] = []
    candidate_count = 0
    cards = soup.select("li.pIav2d")

    # Primary path: parse result cards.
    for card in cards:
        label_node = card.select_one(".JMc5Xc[aria-label]") or card.find(attrs={"aria-label": True})
        if not label_node:
            continue
        label = _norm(label_node.get("aria-label"))
        if not ("Seleziona volo" in label and "Durata totale" in label):
            continue
        candidate_count += 1
        row = _parse_flight_label(label, card.get_text(" ", strip=True), card=card, index=len(rows))
        rows.append(row)

    # Fallback path: parse any aria-labels with flight cards, even if the classes changed.
    if not rows:
        seen_labels = set()
        for node in soup.find_all(attrs={"aria-label": True}):
            label = _norm(node.get("aria-label"))
            if label in seen_labels:
                continue
            if "Seleziona volo" in label and "Durata totale" in label and PRICE_RX.search(label):
                seen_labels.add(label)
                candidate_count += 1
                rows.append(_parse_flight_label(label, "", card=None, index=len(rows)))

    raw_rows_before_dedupe = len(rows)
    df = pd.DataFrame(rows)

    if dedupe and not df.empty:
        # Google can include duplicate copies of the same flight card in the
        # saved HTML. Prefer card_id when available; otherwise use a semantic key.
        if "card_id" in df.columns and df["card_id"].notna().any():
            df = df.drop_duplicates(subset=["card_id"], keep="first")
        else:
            key_cols = [
                c for c in [
                    "price_eur", "airline", "operated_by", "origin", "destination",
                    "depart_time", "arrive_time", "duration_text", "stops_text"
                ]
                if c in df.columns
            ]
            if key_cols:
                df = df.drop_duplicates(subset=key_cols, keep="first")
        df = df.reset_index(drop=True)

    # Add query metadata to every row for traceability.
    for col, value in {
        "query_origin": query_info.get("query_origin"),
        "query_destination": query_info.get("query_destination"),
        "query_depart_date": query_info.get("query_depart_date"),
        "query_text": query_info.get("query_text"),
    }.items():
        if value is not None and not df.empty and col not in df.columns:
            df.insert(0, col, value)

    if not df.empty:
        # Fill flight_id from the explicit Travel Impact itinerary when present.
        # If not present, create a stable parser-generated fallback ID.
        if "flight_id" not in df.columns:
            df["flight_id"] = None
        if "flight_id_source" not in df.columns:
            df["flight_id_source"] = None
        for idx, record in df.iterrows():
            if pd.isna(record.get("flight_id")) or not str(record.get("flight_id")).strip():
                fallback_row = record.to_dict()
                fallback_id = _make_fallback_flight_id(fallback_row)
                df.at[idx, "flight_id"] = fallback_id
                df.at[idx, "flight_id_source"] = "generated_from_card_fields"

    if not include_raw_label and "raw_label" in df.columns:
        df = df.drop(columns=["raw_label"])

    if not df.empty:
        # Stable, useful column order.
        preferred = [
            "query_origin", "query_destination", "query_depart_date",
            "result_index", "flight_id", "flight_id_source", "section",
            "price_eur", "currency", "airline", "operated_by",
            "carrier_code", "flight_number", "flight_departure_date",
            "origin", "destination", "depart_time", "arrive_time", "duration_text",
            "stops_text", "stops_count", "origin_airport_name", "destination_airport_name",
            "depart_day_text", "arrive_day_text", "emissions_kg_co2e",
            "baggage_cabin_not_included", "flight_segments_count", "flight_segment_ids",
            "tim_itinerary", "travelimpactmodel_url", "card_id", "airport_codes_found",
            "query_text", "raw_label",
        ]
        existing = [c for c in preferred if c in df.columns]
        rest = [c for c in df.columns if c not in existing]
        df = df[existing + rest]

        # Sort by price when possible, otherwise preserve page order.
        if "price_eur" in df.columns:
            df = df.sort_values(["price_eur", "result_index"], na_position="last").reset_index(drop=True)

    warnings: List[str] = []
    if df.empty:
        warnings.append(
            "No flight cards were parsed. The HTML may be a consent/error page, "
            "a JS-only bootstrap page, or Google may have changed the markup."
        )
    if query_info.get("query_destination") and not df.empty and "destination" in df.columns:
        dests = sorted({x for x in df["destination"].dropna().astype(str) if x})
        if dests and query_info["query_destination"] not in dests:
            warnings.append(
                f"Parsed destination codes {dests} do not include query destination "
                f"{query_info['query_destination']}. Check that the HTML corresponds to the expected URL."
            )

    info: Dict[str, Any] = {
        "html_path": str(html_path) if html_path else None,
        "html_bytes": len(html_text.encode("utf-8", errors="replace")),
        "cards_li_pIav2d_found": len(cards),
        "flight_card_candidates_found": candidate_count,
        "raw_rows_before_dedupe": int(raw_rows_before_dedupe),
        "dedupe": bool(dedupe),
        "rows_parsed": int(len(df)),
        "empty": bool(df.empty),
        "warnings": warnings,
        **query_info,
    }
    return df, info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True, help="Path to HTML saved from requests.get(...).text")
    ap.add_argument("--out", default=None, help="Optional CSV output path")
    ap.add_argument("--json-out", default=None, help="Optional JSON records output path")
    ap.add_argument("--summary-out", default=None, help="Optional parser summary/diagnostics JSON path")
    ap.add_argument("--no-raw-label", action="store_true", help="Drop the long raw aria-label column")
    ap.add_argument("--no-dedupe", action="store_true", help="Keep duplicate cards if they appear in the HTML")
    ap.add_argument("--print-head", type=int, default=20, help="Rows to print to stdout; 0 disables")
    args = ap.parse_args()

    df, info = parse_google_flights_html(
        args.html,
        include_raw_label=not args.no_raw_label,
        dedupe=not args.no_dedupe,
    )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.out, index=False)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        df.to_json(args.json_out, orient="records", force_ascii=False, indent=2)
    if args.summary_out:
        Path(args.summary_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_out).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(info, ensure_ascii=False, indent=2))
    if args.print_head and not df.empty:
        cols = [c for c in [
            "flight_id", "price_eur", "airline", "operated_by",
            "carrier_code", "flight_number", "origin", "destination",
            "depart_time", "arrive_time", "duration_text", "stops_text",
            "emissions_kg_co2e", "baggage_cabin_not_included",
        ] if c in df.columns]
        print(df[cols].head(args.print_head).to_string(index=False))


if __name__ == "__main__":
    main()
