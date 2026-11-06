"""Acquire cited main articles online; extract text locally without an LLM or OCR.

Only current-run downloads can supply reference evidence. A publisher landing
page, abstract, inaccessible article, or unreadable PDF is not full-text evidence.
"""

import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from runtime.artifacts import read, save, table

POLICY = "online-cited-main-articles-v2"
MAX_BYTES = 40 * 1024 * 1024


class ArticleRetrievalError(RuntimeError):
    """Online retrieval failed without establishing article availability."""


def transient_status(code):
    return not code or code in (408, 429) or code >= 500


def normalized(value):
    return re.sub(r"\W+", "", str(value).casefold())


def adequate(blocks):
    """A minimum text-size gate, not a claim or chemical-content filter."""
    return sum(len(b["text"].split()) for b in blocks) >= 300


def block(text, page=0):
    return {"type": "text", "text": " ".join(text.split()), "page_idx": page}


def extract_main(content, content_type, doi, title):
    """Return text blocks and format; reject unverified or abstract-only pages."""
    if content.lstrip().startswith(b"%PDF-"):
        pages = PdfReader(BytesIO(content)).pages
        blocks = [block(p.extract_text() or "", i) for i, p in enumerate(pages)]
        front = " ".join(b["text"] for b in blocks[:2])
        # Check the article front matter, not DOI occurrences in its references.
        if not (
            (title and normalized(title) in normalized(front))
            or normalized(doi) in normalized(front)
        ):
            raise ValueError("PDF article identity could not be verified")
        if re.search(
            r"\b(supporting information|supplementary information|supplementary material)\b",
            front[:250],
            re.IGNORECASE,
        ):
            raise ValueError("Supplementary material is not a main article")
        kind = "pdf"
    elif "xml" in content_type or content.lstrip().startswith(b"<?xml"):
        root = ElementTree.fromstring(content)
        for node in root.iter():
            node.tag = node.tag.split("}")[-1]
        if root.tag != "article":
            raise ValueError("Response is not a full-text article XML")
        front = root.find("front")
        article_dois = (
            []
            if front is None
            else [
                "".join(n.itertext()).strip().lower()
                for n in front.iter("article-id")
                if n.get("pub-id-type") == "doi"
            ]
        )
        if doi.lower() not in article_dois:
            raise ValueError("XML article DOI does not match")
        body = root.find("body")
        if body is None:
            raise ValueError("XML contains no article body")
        for parent in body.iter():
            for child in list(parent):
                if child.tag in {"supplementary-material", "ref-list"}:
                    parent.remove(child)
        blocks = [
            block(" ".join(n.itertext()))
            for n in body.iter()
            if n.tag in {"title", "p"}
        ]
        kind = "xml"
    else:
        soup = BeautifulSoup(content, "html.parser")
        metadata = soup.select_one(
            'meta[name="citation_doi"], meta[name="dc.Identifier"], meta[name="DC.Identifier"]'
        )
        declared = metadata.get("content", "") if metadata else ""
        if normalized(doi) != normalized(
            re.sub(r"^https?://(?:dx\.)?doi.org/", "", declared)
        ):
            raise ValueError("HTML article DOI does not match")
        # Deliberately avoid a whole-page fallback: abstract pages have navigation
        # and related-article text which can otherwise masquerade as a long body.
        body = soup.select_one(
            '[itemprop="articleBody"], .article-body, .article__body, #article-body, .c-article-body, .hlFld-Fulltext'
        )
        if body is None:
            raise ValueError("No identifiable full-text HTML body")
        for node in body.select(
            "script, style, nav, aside, .references, .supplementary-material"
        ):
            node.decompose()
        if len(body.select("h2, h3, section")) < 2:
            raise ValueError("HTML lacks full article sections")
        blocks = [block(n.get_text(" ", strip=True)) for n in body.select("h2, h3, p")]
        kind = "html"
    blocks = [b for b in blocks if b["text"]]
    if not adequate(blocks):
        raise ValueError("No sufficiently complete readable article body")
    return blocks, kind


class OnlineArticles:
    def __init__(self, controller):
        self.controller = controller
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "PhotocatalysisReferenceReader/1.0"
        self.events = []

    def get(self, url, **kwargs):
        self.controller.check()
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError("Unsupported article URL")
        for attempt in range(1, 4):
            self.controller.check()
            event = {"url": url, "attempt": attempt}
            self.events.append(event)
            try:
                with self.session.get(url, timeout=(10, 35), stream=True, **kwargs) as response:
                    event["status"] = response.status_code
                    response.raise_for_status()
                    chunks, size = [], 0
                    for part in response.iter_content(65536):
                        self.controller.check()
                        size += len(part)
                        if size > MAX_BYTES:
                            raise ValueError("Article response exceeds size limit")
                        chunks.append(part)
                    response._content = b"".join(chunks)
                    response._content_consumed = True
                    return response
            except requests.RequestException as exc:
                code = exc.response.status_code if exc.response is not None else 0
                event.update(status=code, error_type=type(exc).__name__)
                if attempt == 3 or not transient_status(code):
                    raise
                delay = float(attempt)
                if exc.response is not None:
                    retry_after = exc.response.headers.get('Retry-After', '')
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            # Leave longer/date-based cooldowns for a later invocation.
                            raise exc
                if delay > 30:
                    raise
                for _ in range(int(delay * 10)):
                    self.controller.check()
                    time.sleep(0.1)

    def discover(self, doi):
        urls, errors, title = [], [], ""
        successful = 0
        try:
            response = self.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": 'DOI:"' + doi + '"', "format": "json"},
            )
            records = response.json()["resultList"]["result"]
            successful += 1
            for row in records:
                if row.get("doi", "").lower() != doi.lower():
                    continue
                title = row.get("title", "")
                if row.get("pmcid") and row.get("isOpenAccess") == "Y":
                    urls.append(
                        "https://www.ebi.ac.uk/europepmc/webservices/rest/"
                        + row["pmcid"]
                        + "/fullTextXML"
                    )
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(type(exc).__name__ + ": Europe PMC metadata unavailable")
        try:
            row = self.get(
                "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="/")
            ).json()
            if str(row.get("doi", "")).lower() != "https://doi.org/" + doi.lower():
                raise ValueError("OpenAlex DOI mismatch")
            successful += 1
            title = row.get("title") or title
            for location in row.get("locations", []):
                if location.get("is_oa"):
                    # Prefer structured HTML when available; PDFs are the fallback.
                    urls.extend(
                        location[k]
                        for k in ("landing_page_url", "pdf_url")
                        if location.get(k)
                    )
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(type(exc).__name__ + ": OpenAlex metadata unavailable")
        if not successful:
            raise ArticleRetrievalError("All article discovery providers failed for " + doi)
        return title, list(dict.fromkeys(urls)), errors

    def fetch(self, doi):
        title, urls, errors = self.discover(doi)
        # Follow only a publisher's explicitly identified main-article PDF link.
        seen = set()
        transient_errors = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            self.controller.check()
            try:
                response = self.get(url)
                kind = response.headers.get("Content-Type", "")
                if "html" in kind:
                    soup = BeautifulSoup(response.content, "html.parser")
                    doi_meta = soup.select_one('meta[name="citation_doi"]')
                    pdf = soup.select_one('meta[name="citation_pdf_url"]')
                    if (
                        pdf
                        and doi_meta
                        and normalized(doi_meta.get("content", "")) == normalized(doi)
                    ):
                        link = urljoin(response.url, pdf.get("content", ""))
                        if link not in urls:
                            urls.append(link)
                blocks, kind = extract_main(response.content, kind, doi, title)
                return (
                    {
                        "status": "available",
                        "doi": doi,
                        "title": title,
                        "url": response.url,
                        "format": kind,
                    },
                    blocks,
                    response.content,
                )
            except requests.RequestException as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if transient_status(code):
                    transient_errors.append(type(exc).__name__)
                errors.append(type(exc).__name__ + ": article request failed")
            except (ValueError, ElementTree.ParseError, PyPdfError) as exc:
                errors.append(type(exc).__name__ + ": " + str(exc)[:180])
        if transient_errors:
            raise ArticleRetrievalError(
                "Transient article retrieval failure; retry Stage 9: " + doi
            )
        return (
            {
                "status": "unavailable",
                "doi": doi,
                "title": title,
                "url": "",
                "format": "",
                "reason": "no_readable_online_main_article",
                "errors": errors,
            },
            [],
            b"",
        )


def acquire_references(ctx, references):
    """Fetch each DOI once, with successful checkpoints confined to this run."""
    from runtime.library import doi_key

    dois = sorted({doi_key(r.get("reference_doi")) for r in references} - {""})
    work = ctx.work / "online_articles"
    work.mkdir(parents=True, exist_ok=True)

    def acquire(item):
        index, doi = item
        ident = f"ONLINE_REF_{index:08d}"
        checkpoint = work / (ident + ".json")
        cached = read(checkpoint) if checkpoint.exists() else {}
        client = OnlineArticles(ctx.controller)
        try:
            if cached.get("doi") == doi and cached.get("status") in {"available", "unavailable"}:
                record, blocks = cached["record"], cached["blocks"]
                content = (work / (ident + ".source")).read_bytes() if blocks else b''
            else:
                try:
                    record, blocks, content = client.fetch(doi)
                except ArticleRetrievalError as exc:
                    record = {'status': 'retrieval_failed', 'doi': doi, 'title': '',
                              'url': '', 'format': '', 'reason': 'online_retrieval_failed_after_retries',
                              'error': str(exc)}
                    blocks, content = [], b''
                finally:
                    save(work / (ident + ".requests.json"), client.events)
                if record["status"] == "available":
                    (work / (ident + ".source")).write_bytes(content)
                save(checkpoint, {'doi': doi, 'status': record['status'],
                                  'record': record, 'blocks': blocks})
            if not blocks:
                return record, None
            stem = doi.replace("/", "_")
            if any(c in stem for c in '\\:*?"<>|') or stem in {".", ".."}:
                raise ValueError("Unsafe reference DOI filename")
            relative = f"reference_documents/{stem}/auto/{stem}_content_list.json"
            save(ctx.pending / relative, blocks)
            original = (
                ctx.pending / "reference_sources" / (ident + "." + record["format"])
            )
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(content)
            record.update(source_id=ident, output=relative)
            return record, {
                "source_id": ident,
                "relative_path": relative,
                "input_prefix": "outputs/09_library",
                "discovery_doc_ids": "",
                "doi": doi,
                "title": record["title"],
                "has_text": True,
                "doi_conflict": False,
                "online_reference": True,
            }
        finally:
            client.session.close()

    records, sources = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(acquire, item) for item in enumerate(dois, 1)]
        for future in as_completed(futures):
            record, source = future.result()
            records.append(record)
            if source:
                sources.append(source)
            ctx.status(
                state="online_reference_articles",
                completed=len(records),
                total=len(dois),
                retrieval_failed=sum(r['status'] == 'retrieval_failed' for r in records),
            )
    records.sort(key=lambda r: r['doi'])
    sources.sort(key=lambda r: r['source_id'])
    # Errors and request traces are runtime details, not scientific output columns.
    save(work / "acquisition.json", records)
    fields = [
        "doi",
        "status",
        "title",
        "url",
        "format",
        "source_id",
        "output",
        "reason",
    ]
    table(
        ctx.pending / "reference_acquisition.csv",
        [{k: r.get(k, "") for k in fields} for r in records],
        fields,
    )
    counts = dict(Counter(r['status'] for r in records))
    print('[Stage 9] Reference acquisition: ' + str(counts), flush=True)
    if records and counts.get('retrieval_failed', 0) == len(records):
        raise RuntimeError('All reference acquisitions failed; retry Stage 9 after network recovery')
    return sources, {r["doi"]: r for r in records if r["status"] == "available"}
