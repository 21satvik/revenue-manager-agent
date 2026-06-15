"""Extract: scrape the client-rendered data site with Playwright (Chromium).

The data site renders with JavaScript, so a plain HTTP GET returns an empty shell
- we drive a real browser and wait for the *data* to render (not just any row,
which briefly shows a "Loading…" placeholder). The flow is:

1. Paginate ``/reservations`` (100 rows/page) via the "Next →" control and collect
   every reservation id. Pagination is a client-side button, there is no working
   ``?page=`` query param, so we click Next and wait for the rows to swap.
2. Open each ``/reservations/<id>`` detail page for the reservation-level fields
   (rendered as ``[data-field]`` dt/dd pairs) and the per-night stay-rows table
   (which carries the per-night ``financial_status``, ``property_date`` and the
   daily revenue columns).
3. Read ``/reference``, a *tabbed* page, clicking each of the five tabs
   (Room types, Markets, Channels, Rate plans, Macro history) to capture every
   lookup table plus ``market_macro_group_history``.
4. Read ``/verify`` for the current ``dataset_revision``.

Selectors below were verified against the live DOM (dataset_revision
2026.06.12.2). They are isolated as module constants so re-pinning them after a
site change is a one-line edit.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://otel-hackathon-data-site.vercel.app"
PAGE_SIZE = 100  # data site shows 100 reservations per list page
RENDER_TIMEOUT_MS = 30_000
SELECTOR_TIMEOUT_MS = 15_000  # wait for the readiness selector before reading HTML

# Selectors verified against the live DOM
SEL_RES_LINK = "a[href^='/reservations/']"  # a *rendered* reservation row (not "Loading…")
SEL_DATA_FIELD = "[data-field]"  # detail-page key/value pairs (dt label + dd value)
SEL_TABLE_ROW = "table tbody tr"  # a rendered table row
SEL_NEXT = "button:has-text('Next')"  # list pagination "Next →" control

# Reference tab label -> (canonical lookup table name, signature column).
# The signature column uniquely identifies the tab's table once it has rendered,
# so we never read a stale/loading table after switching tabs. Markets and Macro
# history both start with market_code, so we key off a distinctive later column.
REFERENCE_TABS = {
    "Room types": ("room_type_lookup", "space_type"),
    "Markets": ("market_code_lookup", "market_name"),
    "Channels": ("channel_code_lookup", "channel_code"),
    "Rate plans": ("rate_plan_lookup", "rate_plan_code"),
    "Macro history": ("market_macro_group_history", "valid_from"),
}

# The site renders missing values as an em-dash; "" guards a blank cell.
NULLISH = {"", "—"}
_REVISION_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2}\.\d+)")


@dataclass
class ScrapeResult:
    reservation_ids: list[str] = field(default_factory=list)
    pages_scraped: int = 0
    details: list[dict] = field(default_factory=list)
    reference: dict = field(default_factory=dict)
    dataset_revision: str | None = None


@contextmanager
def _browser(headless: bool = True):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()


def _clean(text: str | None) -> str | None:
    """Strip whitespace and map the site's null glyph (``—``) to None."""
    if text is None:
        return None
    stripped = text.strip()
    return None if stripped.lower() in NULLISH else stripped


def _goto(page, url: str, ready_selector: str | None) -> None:
    """Navigate and wait for the specific rendered element before reading the DOM.

    ``networkidle`` never settles on this SPA, so we wait on the concrete
    ``ready_selector`` (e.g. a reservation link, a data-field, a table row) which
    only appears *after* the client has replaced the "Loading…" placeholder.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
    if ready_selector:
        try:
            page.wait_for_selector(ready_selector, timeout=SELECTOR_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - some pages legitimately lack the selector
            pass


def _soup(page) -> BeautifulSoup:
    return BeautifulSoup(page.content(), "html.parser")


def _rows_to_dicts(table) -> list[dict]:
    """Turn an HTML <table> into a list of {lower_snake_header: cleaned_value}.

    Headers are lower-cased so they match the schema/transform column names
    (the site renders them upper-case, e.g. ``STAY_DATE``); empty rows are skipped.
    """
    headers = [th.get_text(strip=True).lower() for th in table.select("thead th")]
    out: list[dict] = []
    for tr in table.select("tbody tr"):
        cells = [_clean(td.get_text(strip=True)) for td in tr.select("td")]
        if not any(cells):
            continue  # skip placeholder / empty rows
        out.append(dict(zip(headers, cells, strict=False)))
    return out


def scrape_reservation_ids(page) -> tuple[list[str], int]:
    """Paginate the list via "Next →" and return (sorted unique ids, pages_scraped).

    There is no working ``?page=`` param, so we click the Next button and wait for
    the first row id to change before reading the next page. Stops when the Next
    control is absent/disabled or a page yields no new ids.
    """
    ids: list[str] = []
    seen: set[str] = set()
    pages = 0
    _goto(page, f"{BASE_URL}/reservations", SEL_RES_LINK)
    while True:
        soup = _soup(page)
        page_ids = [a["href"].rsplit("/", 1)[-1] for a in soup.select(SEL_RES_LINK)]
        new_ids = [i for i in page_ids if i not in seen]
        for i in new_ids:
            seen.add(i)
            ids.append(i)
        pages += 1

        nxt = page.locator(SEL_NEXT).first
        if not page_ids or not new_ids or nxt.count() == 0 or not nxt.is_enabled():
            break

        first_before = page_ids[0]
        nxt.click()
        try:
            # Wait until the client swaps the rows (first row id differs).
            page.wait_for_function(
                "prev => { const a = document.querySelector(\"a[href^='/reservations/']\");"
                " return a && a.getAttribute('href').split('/').pop() !== prev; }",
                arg=first_before,
                timeout=SELECTOR_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001 - no further page rendered
            break
    return sorted(set(ids)), pages


def scrape_reservation_detail(page, reservation_id: str, attempts: int = 3) -> dict:
    """Scrape one /reservations/<id> detail page into a raw dict.

    Reservation-level fields come from ``[data-field]`` blocks (each is a div with
    a ``dt`` label and a ``dd`` value, we read the ``dd``). The per-night
    ``stay_nights`` list comes from the detail table, whose columns include the
    per-night ``financial_status``, ``property_date`` and daily revenues.

    The detail page occasionally hydrates slowly; we retry the navigation until the
    ``[data-field]`` blocks are present and raise if a page never renders, so a
    transient render miss can never silently drop a reservation from the load.
    """
    last_error: Exception | None = None
    for _ in range(attempts):
        page.goto(f"{BASE_URL}/reservations/{reservation_id}", wait_until="domcontentloaded",
                  timeout=RENDER_TIMEOUT_MS)
        try:
            page.wait_for_selector(SEL_DATA_FIELD, timeout=SELECTOR_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - slow hydration; retry the navigation
            last_error = exc
            continue
        soup = _soup(page)
        blocks = soup.select(SEL_DATA_FIELD)
        if not blocks:
            continue
        detail: dict = {"reservation_id": reservation_id}
        for block in blocks:
            value_el = block.select_one("dd") or block
            detail[block["data-field"]] = _clean(value_el.get_text(strip=True))

        # The per-night table is the one whose header includes stay_date.
        night_table = None
        for table in soup.select("table"):
            headers = [th.get_text(strip=True).lower() for th in table.select("thead th")]
            if "stay_date" in headers:
                night_table = table
                break
        detail["stay_nights"] = _rows_to_dicts(night_table) if night_table else []
        return detail

    raise RuntimeError(
        f"/reservations/{reservation_id} never rendered detail fields"
    ) from last_error


def _read_active_table(page) -> list[dict]:
    """Read the currently-rendered /reference table via locators into row dicts."""
    table = page.locator("table").first
    headers = [h.strip().lower() for h in table.locator("thead th").all_inner_texts()]
    rows: list[dict] = []
    body = table.locator("tbody tr")
    for i in range(body.count()):
        cells = [_clean(c) for c in body.nth(i).locator("td").all_inner_texts()]
        if any(cells):
            rows.append(dict(zip(headers, cells, strict=False)))
    return rows


def scrape_reference(page) -> dict:
    """Scrape the tabbed /reference page into {canonical_table_name: [row dicts]}.

    The page shows one lookup table at a time behind a tab button. We click each
    tab and then block on ``wait_for_function`` until the DOM table actually
    carries that tab's signature column *and* a data row, a deterministic wait
    that defeats the render race (a time-based sleep was flaky). A tab that yields
    no rows raises, so a transient never silently loads an incomplete reference.
    """
    _goto(page, f"{BASE_URL}/reference", SEL_TABLE_ROW)
    reference: dict[str, list[dict]] = {}
    for label, (canonical, signature) in REFERENCE_TABS.items():
        page.locator(f"button:has-text('{label}')").first.click(timeout=SELECTOR_TIMEOUT_MS)
        # Wait until the active table's headers include the signature column and
        # at least one body row is present (i.e. the new tab has fully rendered).
        page.wait_for_function(
            """sig => {
              const t = document.querySelector('table');
              if (!t) return false;
              const ths = [...t.querySelectorAll('thead th')]
                .map(e => e.textContent.trim().toLowerCase());
              const rows = t.querySelectorAll('tbody tr').length;
              return ths.includes(sig) && rows > 0;
            }""",
            arg=signature,
            timeout=SELECTOR_TIMEOUT_MS,
        )
        rows = _read_active_table(page)
        if not rows:
            raise RuntimeError(f"/reference tab {label!r} rendered no rows")
        reference[canonical] = rows
    return reference


def scrape_dataset_revision(page) -> str | None:
    """Read the current ``dataset_revision`` (e.g. ``2026.06.12.2``) from /verify."""
    _goto(page, f"{BASE_URL}/verify", None)
    try:
        # /verify computes checksums client-side; wait for the dotted revision token.
        revision_text = "text=/\\d{4}\\.\\d{2}\\.\\d{2}\\.\\d+/"
        page.wait_for_selector(revision_text, timeout=SELECTOR_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        pass
    match = _REVISION_RE.search(page.locator("body").inner_text())
    return match.group(1) if match else None


def scrape_all(headless: bool = True, limit: int | None = None) -> ScrapeResult:
    """Run the full extract. ``limit`` caps reservations for smoke runs."""
    result = ScrapeResult()
    with _browser(headless=headless) as page:
        result.dataset_revision = scrape_dataset_revision(page)
        result.reference = scrape_reference(page)
        ids, pages = scrape_reservation_ids(page)
        result.reservation_ids = ids
        result.pages_scraped = pages
        for rid in ids[: limit or len(ids)]:
            result.details.append(scrape_reservation_detail(page, rid))
    return result
