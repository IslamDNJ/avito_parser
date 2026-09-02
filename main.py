"""
main.py — entry point for the HONEX Avito mini-parser test task.

Usage:
    python main.py                     # live run against avito.ru
    python main.py --test-mode         # offline demo using fixtures/*.html
    python main.py --output result.xlsx

By default this script tries to reach Avito directly. Avito aggressively
blocks non-browser traffic (often returning a CAPTCHA/403 to a plain
`requests` client), and this task explicitly does not require bypassing
that protection. If a live request is blocked, use --test-mode, which
runs the exact same parsing/filtering/ranking code against local sample
HTML files in fixtures/ so the pipeline can still be reviewed end-to-end.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

import requests

from avito_parser import Listing, search_article

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("main")

# Артикулы из задания: (артикул, описание)
ARTICLES: list[tuple[str, str]] = [
    ("223112R020", "Прокладка головки блока цилиндра"),
    ("233002F700", "Балансирный вал в сборе"),
]

FIXTURES_DIR = Path(__file__).parent / "fixtures"

FIELDNAMES = [
    "article",
    "query",
    "title",
    "price",
    "region",
    "condition",
    "url",
    "rank",
    "status",
    "checked_at",
]


def listing_to_row(item: Listing) -> dict:
    return {
        "article": item.article,
        "query": item.query,
        "title": item.title,
        "price": item.price if item.price is not None else "",
        "region": item.region,
        "condition": item.condition,
        "url": item.url,
        "rank": item.rank if item.rank is not None else "",
        "status": item.status,
        "checked_at": item.checked_at,
    }


def run(test_mode: bool) -> list[Listing]:
    session = requests.Session()
    all_rows: list[Listing] = []

    for article, description in ARTICLES:
        logger.info("Searching article %s (%s)", article, description)

        html_override = None
        if test_mode:
            fixture_path = FIXTURES_DIR / f"{article}.html"
            if fixture_path.exists():
                html_override = fixture_path.read_text(encoding="utf-8")
            else:
                logger.warning("No fixture found for %s, treating as empty result", article)
                html_override = "<html></html>"

        results = search_article(article, description, session=session, html_override=html_override)
        for r in results:
            logger.info("  -> %s | %s | %s", r.status, r.price, r.title[:60])
        all_rows.extend(results)

    return all_rows


def write_csv(rows: list[Listing], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for item in rows:
            writer.writerow(listing_to_row(item))


def write_xlsx(rows: list[Listing], path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "avito_results"
    ws.append(FIELDNAMES)
    for item in rows:
        row = listing_to_row(item)
        ws.append([row[f] for f in FIELDNAMES])
    wb.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="HONEX Avito mini-parser")
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Use local HTML fixtures instead of live requests to avito.ru",
    )
    parser.add_argument(
        "--output",
        default="result.csv",
        help="Output file path (.csv or .xlsx), default: result.csv",
    )
    args = parser.parse_args()

    rows = run(test_mode=args.test_mode)

    out_path = Path(args.output)
    if out_path.suffix.lower() == ".xlsx":
        write_xlsx(rows, out_path)
    else:
        write_csv(rows, out_path)

    logger.info("Saved %s rows to %s", len(rows), out_path)


if __name__ == "__main__":
    main()
