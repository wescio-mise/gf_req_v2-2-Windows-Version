#!/usr/bin/env python
"""Salva i cookies di Google Flights senza dipendenze da Jupyter"""

import asyncio
import sys

# Su Windows con Python 3.14, usa la policy ProactorEventLoopPolicy
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from gf_ck_gen import save_google_flights_landing

async def main():
    url = "https://www.google.com/travel/flights?gl=IT&hl=it&q=Flights%20to%20PMO%20from%20FCO%20on%202026-08-09%20oneway%20economy%20nonstops"
    result = await save_google_flights_landing(url, headless=False, out_dir="gf_landing_artifacts")
    print("\n✅ Cookies salvati con successo!")
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
