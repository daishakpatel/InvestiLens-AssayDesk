"""Phase 7c, offline: 8-K item codes, 10-Q Part I, exhibits, extra forms, submission shards.

Literal heading strings come from real 8-Ks (dash, non-breaking space, trailing period, upper
case, table-of-contents copy). Live-source behaviour runs against `httpx.MockTransport` with
frozen EDGAR responses. Refs: FR-EX-001 … FR-EX-014, DR-EX-001 … DR-EX-003.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.config import Settings, get_settings
from app.ingestion.documents.blocks import Block
from app.ingestion.documents.chunker import FilingMeta
from app.ingestion.documents.eightk_items import item_codes_in_document, parse_item_heading
from app.ingestion.documents.pipeline import chunk_filing_html
from app.ingestion.documents.sections import detect_sections, section_key
from app.ingestion.sec.pipeline import _listing_since, _within_backfill
from app.providers.mocks._fixtures import fixture_gzip_bytes, fixture_path
from app.providers.sec.base import FilingRef, is_press_release_exhibit
from app.providers.sec.live import ALLOWED_HOSTS, LiveSecSource, parse_index_headers
from app.providers.sec.mock import MockSecSource
from app.utils.http import HardenedHttpClient
from app.utils.storage import FilesystemObjectStorage, ObjectStorage

NVDA = "0001045810"


# Non-breaking spaces and en/em dashes are written literally: they are the cases under test.
@pytest.mark.parametrize(
    ("text", "code", "title"),
    [
        ("Item 5.01 Changes in Control of Registrant.", "5.01", "Changes in Control of Registrant"),
        ("Item 5.02.", "5.02", ""),  # trailing period, title on the next line
        ("ITEM 5.07 SUBMISSION OF MATTERS TO A VOTE", "5.07", "SUBMISSION OF MATTERS TO A VOTE"),
        ("Item 2.02 – Results of Operations", "2.02", "Results of Operations"),  # noqa: RUF001
        ("Item 2.06 — Material Impairments.", "2.06", "Material Impairments"),
        (
            "Item 4.02: Non-Reliance on Previously Issued",
            "4.02",
            "Non-Reliance on Previously Issued",
        ),
        ("Item 5.02. Departure of Directors", "5.02", "Departure of Directors"),  # noqa: RUF001
        (
            "Item 9.01 - Financial Statements and Exhibits.",
            "9.01",
            "Financial Statements and Exhibits",
        ),
    ],
)
def test_8k_headings_parse_to_the_exact_dotted_code(text: str, code: str, title: str) -> None:
    # FR-EX-001/002: 5.01, 5.02 and 5.07 stay distinct (they used to collapse to "5").
    assert parse_item_heading(text) == (code, title)


@pytest.mark.parametrize(
    "text",
    [
        "Item 5",  # a 10-K heading, no dotted suffix
        "Item 5.021 is not an item",
        "Pursuant to Item 5.02 the Company reports " + "x" * 200,  # a sentence, not a heading
        "See Item 2.02 above.",  # mentions an item mid-sentence
    ],
)
def test_non_headings_are_rejected(text: str) -> None:
    assert parse_item_heading(text) is None


def _text(*lines: str) -> list[Block]:
    return [Block(kind="text", text=line) for line in lines]


def test_table_of_contents_copy_gives_way_to_the_real_heading() -> None:
    # FR-EX-002: a TOC entry ("…Officers 3", page number) then the heading within 300 characters.
    blocks = _text(
        "Table of Contents",
        "Item 5.02 Departure of Directors or Certain Officers 3",
        "Item 9.01 Financial Statements and Exhibits 4",
        "Item 5.02 Departure of Directors or Certain Officers",
        "On July 1, 2026 the Chief Financial Officer resigned.",
        "Item 9.01 Financial Statements and Exhibits",
        "(d) Exhibits.",
    )
    sections = detect_sections(blocks, "8-K").sections
    assert [(s.item_code, s.start_idx) for s in sections] == [("5.02", 3), ("9.01", 5)]
    assert sections[0].key == "8k_item5.02"


def test_real_8k_with_items_502_and_901_yields_two_sections() -> None:
    # FR-EX-001: NVIDIA's 2026-03-06 8-K (Items 5.02 and 9.01).
    html = fixture_gzip_bytes("nvda", "8k_502.htm.gz").decode()
    rows = chunk_filing_html(html, FilingMeta(7, 1, "NVIDIA", "8-K", "2026-03-02"))
    assert {r["section"].split(".")[0] + "." + r["section"].split(".")[1] for r in rows} == {
        "Item 5.02",
        "Item 9.01",
    }
    # DR-EX-001: the chunk ID carries the full code, so it cannot collide with an old "7_5_0".
    assert all(r["paragraph_id"].startswith(("7_5.02_", "7_9.01_")) for r in rows)


def test_item_codes_from_raw_html() -> None:
    # FR-EX-003 fallback: any case, `&nbsp;`, markup between "Item" and the code.
    html = b"<p>ITEM&nbsp;5.02 Departure</p><p>item 9.01</p><p>Item <b>5.07</b></p>"
    assert item_codes_in_document(html) == ["5.02", "5.07", "9.01"]


def test_10q_part_one_item_one_is_chunked_with_its_own_key() -> None:
    # FR-EX-012 regression: Part II's "Item 1" used to overwrite Part I's, so the financial
    # statements, their notes and MD&A were never chunked.
    html = fixture_gzip_bytes("nvda", "10q.htm.gz").decode()
    rows = chunk_filing_html(html, FilingMeta(9, 1, "NVIDIA", "10-Q", "2026-07-26"))
    notes = [r for r in rows if r["chunk_metadata"]["section_key"] == "10q_part1_item1_notes"]
    assert notes and notes[0]["section"].startswith("Part I, Item 1. Financial Statements")
    assert any("Notes to Condensed Consolidated Financial Statements" in r["text"] for r in notes)
    keys = {r["chunk_metadata"]["section_key"] for r in rows}
    assert {"10q_part1_item2", "10q_part2_item1", "10q_part2_item1a"} <= keys


def test_10q_without_part_one_headings_keeps_the_text_and_is_flagged() -> None:
    # No Part I item heading and none of JPM's own titles either: keep, never drop, and flag.
    blocks = _text("Cover", "Consolidated balance sheets", "Part II", "Item 1. Legal Proceedings")
    result = detect_sections(blocks, "10-Q")
    assert [s.item_code for s in result.sections] == ["P1-0", "P2-1"]
    assert result.sections[0].key == "10q_part1_unitemized" and result.low_confidence


def test_documents_without_items_are_one_section() -> None:
    result = detect_sections(_text("Proxy Statement", "Item 1 is not a heading here"), "DEF 14A")
    assert [s.item_code for s in result.sections] == ["0"] and not result.low_confidence
    assert section_key("DEF 14A", "0") == "document" and section_key("10-K", "7A") == "10k_item7a"
    eight_k = detect_sections(_text("Press release"), "8-K")
    assert eight_k.low_confidence  # an 8-K with no item heading is suspicious


def test_press_release_table_keeps_each_number_with_its_label() -> None:
    # FR-EX-008: NVIDIA's Q2 FY27 EX-99.1. Revenue and diluted EPS land in the same table row,
    # of the same chunk, as their labels.
    html = fixture_gzip_bytes("nvda", "8k_earnings_ex-99.1.htm.gz").decode()
    rows = chunk_filing_html(html, FilingMeta(5, 1, "NVIDIA", "EX-99.1", "2026-08-26", tier=2))
    tables = [r["text"] for r in rows if r["chunk_type"] == "table"]
    assert any(
        "| Revenue | $96,221 |" in t and "| Diluted earnings per share | $2.46 |" in t
        for t in tables
    )
    assert all(r["tier"] == 2 and r["filing_type"] == "EX-99.1" for r in rows)


def test_index_headers_list_each_document_type() -> None:
    # FR-EX-005: EDGAR has no per-filing -index.json; the SGML header names each document.
    page = (
        "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;8-K\n&lt;SEQUENCE&gt;1\n&lt;FILENAME&gt;nvda-8k.htm\n"
        "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-99.1\n&lt;SEQUENCE&gt;2\n&lt;FILENAME&gt;pr.htm\n"
        "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-10.1\n&lt;SEQUENCE&gt;3\n&lt;FILENAME&gt;comp.htm\n"
    )
    assert parse_index_headers(page) == [
        ("8-K", "nvda-8k.htm"),
        ("EX-99.1", "pr.htm"),
        ("EX-10.1", "comp.htm"),
    ]
    assert is_press_release_exhibit("EX-99.2") and is_press_release_exhibit("ex-99")
    assert not is_press_release_exhibit("EX-10.1") and not is_press_release_exhibit("EX-101.SCH")


def _live(
    handler: httpx.MockTransport, storage: ObjectStorage | None = None
) -> tuple[LiveSecSource, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler.handle_request(request)

    settings = get_settings()
    client = HardenedHttpClient(
        user_agent=settings.sec_user_agent,
        allowed_hosts=ALLOWED_HOSTS,
        rate_per_sec=1000.0,
        client=httpx.Client(
            transport=httpx.MockTransport(record),
            headers={"User-Agent": settings.sec_user_agent},
        ),
    )
    return LiveSecSource(client=client, storage=storage), seen


def _submissions(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[-1]
    if name == f"CIK{NVDA}.json":
        return httpx.Response(
            200, content=fixture_path("sec", "submissions_nvda.json").read_bytes()
        )
    if name == "CIK0001045810-submissions-001.json":
        shard = fixture_path("sec", "submissions_nvda_shard.json").read_bytes()
        return httpx.Response(200, content=shard)
    return httpx.Response(404)


def test_a_filing_only_in_an_older_shard_is_found() -> None:
    # FR-EX-011: the 2020-08-19 10-Q is only in `filings.files[0]`, not in `filings.recent`.
    source, seen = _live(httpx.MockTransport(_submissions))
    recent_only = source.list_filings(NVDA)
    assert "0001045810-20-000147" not in {f.accession_number for f in recent_only}

    filings = source.list_filings(NVDA, since=date(2020, 1, 1))
    old = next(f for f in filings if f.accession_number == "0001045810-20-000147")
    assert (old.form, old.filing_date) == ("10-Q", "2020-08-19")
    assert [f.filing_date for f in filings] == sorted(
        (f.filing_date for f in filings), reverse=True
    )
    assert any(r.url.path.endswith("-submissions-001.json") for r in seen)
    assert all(r.headers["User-Agent"] == get_settings().sec_user_agent for r in seen)

    seen.clear()
    source.list_filings(NVDA, since=date(2021, 1, 1))  # shard ends 2020-09-15: not fetched
    assert not any(r.url.path.endswith("-submissions-001.json") for r in seen)


def test_an_older_shard_is_read_from_sec_once(tmp_path: Path) -> None:
    # The pages behind `filings.files[]` are fixed history: a second ingest reads the stored copy.
    source, seen = _live(httpx.MockTransport(_submissions), FilesystemObjectStorage(tmp_path))
    first = source.list_filings(NVDA, since=date(2020, 1, 1))
    seen.clear()
    again = source.list_filings(NVDA, since=date(2020, 1, 1))
    assert again == first
    assert not any(r.url.path.endswith("-submissions-001.json") for r in seen)


def test_live_exhibits_come_from_the_filing_index() -> None:
    # FR-EX-005: EX-99.x only (EX-10.1 ignored), with sizes from index.json, User-Agent sent.
    folder = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073"
    header = (
        "&lt;TYPE&gt;8-K\n&lt;SEQUENCE&gt;1\n&lt;FILENAME&gt;nvda-20260826.htm\n"
        "&lt;TYPE&gt;EX-99.1\n&lt;SEQUENCE&gt;2\n&lt;FILENAME&gt;q2fy27pr.htm\n"
        "&lt;TYPE&gt;EX-99.2\n&lt;SEQUENCE&gt;3\n&lt;FILENAME&gt;q2fy27cfocommentary.htm\n"
        "&lt;TYPE&gt;EX-10.1\n&lt;SEQUENCE&gt;4\n&lt;FILENAME&gt;comp.htm\n"
    )
    listing = {"directory": {"item": [{"name": "q2fy27pr.htm", "size": "341113"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if path.endswith("-index-headers.html"):
            return httpx.Response(200, text=header)
        if path.endswith("/index.json"):
            return httpx.Response(200, json=listing)
        if path.endswith("q2fy27pr.htm"):
            return httpx.Response(200, content=b"<p>press release</p>")
        return httpx.Response(404)

    source, seen = _live(httpx.MockTransport(handler))
    filing = FilingRef(
        cik=NVDA,
        form="8-K",
        accession_number="0001045810-26-000073",
        primary_document="nvda-20260826.htm",
        filing_date="2026-08-26",
        report_date="2026-08-26",
        primary_doc_url=f"{folder}/nvda-20260826.htm",
    )
    exhibits = source.list_exhibits(filing)
    assert [(e.exhibit_type, e.filename, e.size_bytes) for e in exhibits] == [
        ("EX-99.1", "q2fy27pr.htm", 341113),
        ("EX-99.2", "q2fy27cfocommentary.htm", None),
    ]
    assert source.fetch_exhibit(exhibits[0]) == b"<p>press release</p>"
    assert seen[0].headers["User-Agent"] == get_settings().sec_user_agent


def test_live_source_uses_the_declared_user_agent_and_rate_limit() -> None:
    # FR-EX-005 / LGL-002: the default client carries SEC_USER_AGENT and the fair-access rate.
    source = LiveSecSource()
    settings = get_settings()
    http = source._http
    assert http._client.headers["User-Agent"] == settings.sec_user_agent
    assert http._limiter._min_interval == pytest.approx(1 / settings.sec_rate_limit_per_sec)
    assert http._client.timeout.read is not None and http._max_retries >= 1


def test_live_exhibit_listing_with_no_press_release_makes_one_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="&lt;TYPE&gt;8-K\n&lt;SEQUENCE&gt;1\n&lt;FILENAME&gt;a.htm\n"
        )

    source, seen = _live(httpx.MockTransport(handler))
    filing = FilingRef(
        NVDA, "8-K", "a-1", "a.htm", "2026-01-01", None, "https://www.sec.gov/x/a.htm"
    )
    assert source.list_exhibits(filing) == [] and len(seen) == 1


def test_mock_source_serves_items_acceptance_time_and_exhibits() -> None:
    # FR-EX-003/005: the fixture manifest carries EDGAR's own item list and exhibits.
    source = MockSecSource()
    filings = {f.accession_number: f for f in source.list_filings(NVDA)}
    earnings = filings["0001045810-26-000073"]
    assert earnings.items == ("2.02", "9.01") and earnings.accepted_at
    exhibits = source.list_exhibits(earnings)
    assert [e.exhibit_type for e in exhibits] == ["EX-99.1", "EX-99.2"]
    assert b"NVIDIA" in source.fetch_exhibit(exhibits[0])
    assert source.list_exhibits(filings["0001045810-26-000078"]) == []  # 8.01, no EX-99
    with pytest.raises(FileNotFoundError):
        source.fetch_exhibit(exhibits[0].__class__("a", "EX-99.1", "f", "u"))
    assert not source.list_exhibits(
        FilingRef("0000000001", "8-K", "x", "d", "2026-01-01", None, "u")
    )


def test_backfill_windows_per_form() -> None:
    # FR-EX-010: 10-K 5y, 10-Q 12 quarters, 8-K 2y, DEF 14A 3y, NT forms 3y, from config.
    settings = Settings(backfill_proxy_years=3, backfill_nt_years=3, backfill_8k_months=24)
    today = date(2026, 10, 22)

    def ref(form: str, filed: str) -> FilingRef:
        return FilingRef("1", form, "a", "d", filed, None, "u")

    assert _within_backfill(ref("DEF 14A", "2024-05-01"), settings, today)
    assert not _within_backfill(ref("DEF 14A", "2023-05-01"), settings, today)
    assert _within_backfill(ref("DEFA14A", "2025-05-01"), settings, today)
    assert _within_backfill(ref("NT 10-K", "2024-03-27"), settings, today)
    assert not _within_backfill(ref("NT 10-Q", "2023-06-02"), settings, today)
    assert _within_backfill(ref("8-K/A", "2025-01-10"), settings, today)
    assert not _within_backfill(ref("8-K", "2024-09-01"), settings, today)
    assert not _within_backfill(ref("4", "2026-10-01"), settings, today)  # Form 4: out of scope
    assert _within_backfill(ref("10-K", ""), settings, today)  # undated: kept
    assert _listing_since(settings, today) == date(2021, 10, 22)  # the 10-K window is longest
    narrow = Settings(backfill_10k_years=1, backfill_proxy_years=4)
    assert _listing_since(narrow, today) == date(2022, 10, 22)


def test_fixture_manifest_marks_synthetic_forms() -> None:
    # DR-EX-003: NVIDIA never filed NT 10-K / NT 10-Q or a recent 8-K/A; those fixtures say so.
    manifest = json.loads(fixture_path("nvda", "filings_manifest.json").read_text())
    synthetic = {f["form"] for f in manifest["filings"] if f.get("synthetic")}
    assert synthetic == {"NT 10-K", "NT 10-Q", "8-K/A"}
    assert b"SYNTHETIC TEST FIXTURE" in fixture_gzip_bytes("nvda", "nt10k.htm.gz")
