#!/usr/bin/env python3
"""Collect official documents as unreviewed evidence; never create published entries."""
import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

from publisher import MAX_SOURCE_BYTES, OfficialRedirect, official, sha

ROOT = Path(__file__).resolve().parent
USER_AGENT = "LatticeworkRightsCollector/1.0 (official legal source audit)"
MAX_TEXT_BYTES = 40 * 1024 * 1024
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_robots = {}


class PdfLinks(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url, _ = urllib.parse.urldefrag(urllib.parse.urljoin(self.base, href))
        parsed = urllib.parse.urlparse(url)
        if official(url) and parsed.path.lower().endswith(".pdf"):
            self.current = {"url": url, "parts": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None:
            self.links.append({"url": self.current["url"], "label": " ".join(" ".join(self.current["parts"]).split())[:300]})
            self.current = None


def discover(observations, source_dir, output):
    """List candidate official PDFs found on source landing pages; do not download them."""
    report = json.loads(Path(observations).read_text(encoding="utf-8"))
    candidates = {}
    for source in report.get("sources", []):
        if source.get("status") != "observed" or not ID.fullmatch(source.get("id", "")):
            continue
        if "html" not in source.get("content_type", "").lower():
            continue
        snapshot = Path(source_dir) / (source["id"] + ".snapshot")
        if not snapshot.is_file():
            continue
        parser = PdfLinks(source.get("final_url") or source["url"])
        parser.feed(snapshot.read_bytes().decode("utf-8", errors="replace"))
        for link in parser.links:
            item = candidates.setdefault(link["url"], {
                "id": "candidate-" + hashlib.sha256(link["url"].encode()).hexdigest()[:20],
                "url": link["url"], "label": link["label"], "source_ids": [],
                "review_status": "unreviewed_candidate",
            })
            if source["id"] not in item["source_ids"]:
                item["source_ids"].append(source["id"])
    result = {"schema_version": 1, "candidates": sorted(candidates.values(), key=lambda c: c["url"])}
    Path(output).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Found {len(candidates)} official PDF candidates; none were published.")


def check_robots(url):
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots:
        robots_url = origin + "/robots.txt"
        parser = urllib.robotparser.RobotFileParser(robots_url)
        try:
            request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.build_opener(OfficialRedirect()).open(request, timeout=15) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError("robots.txt exceeds size limit")
                parser.parse(data.decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise ValueError(f"robots.txt unavailable (HTTP {exc.code})") from exc
            parser.parse([])
        _robots[origin] = parser
    if not _robots[origin].can_fetch(USER_AGENT, url):
        raise ValueError("source robots.txt disallows collection")


class DocumentRedirect(OfficialRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official(newurl):
            raise ValueError("document redirect left official domains")
        check_robots(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_pdf(url):
    if not official(url):
        raise ValueError("document URL is not an official HTTPS URL")
    check_robots(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.build_opener(DocumentRedirect()).open(request, timeout=30) as response:
        if not official(response.geturl()):
            raise ValueError("document redirect left official domains")
        data = response.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("document exceeds collection size limit")
        if b"%PDF-" not in data[:1024]:
            raise ValueError("response is not a PDF")
        return data, response.geturl(), response.headers.get("Content-Type", "")


def extract_pdf(raw_path, text_path):
    executable = shutil.which("pdftotext")
    poppler_error = None
    if executable:
        try:
            subprocess.run([executable, "-layout", "-enc", "UTF-8", str(raw_path), str(text_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60, check=True)
            if text_path.stat().st_size > MAX_TEXT_BYTES:
                text_path.unlink()
                return {"status": "text_exceeds_limit"}
            return describe_text(text_path, "pdftotext")
        except (OSError, subprocess.SubprocessError) as exc:
            text_path.unlink(missing_ok=True)
            poppler_error = str(exc)
    if not executable or poppler_error is not None:
        try:
            import pypdfium2 as pdfium
        except ImportError:
            return {"status": "extraction_failed", "error": poppler_error} if poppler_error else {"status": "extractor_unavailable"}
        try:
            document = pdfium.PdfDocument(str(raw_path))
            total = 0
            try:
                with text_path.open("wb") as output:
                    for index in range(len(document)):
                        page = document[index]
                        try:
                            textpage = page.get_textpage()
                            try:
                                content = (textpage.get_text_range() + "\f").encode("utf-8")
                            finally:
                                textpage.close()
                        finally:
                            page.close()
                        total += len(content)
                        if total > MAX_TEXT_BYTES:
                            raise ValueError("text exceeds extraction limit")
                        output.write(content)
            finally:
                document.close()
            return describe_text(text_path, "pdfium")
        except Exception as exc:
            text_path.unlink(missing_ok=True)
            return {"status": "extraction_failed", "error": str(exc)}
def describe_text(text_path, extractor):
    data = text_path.read_bytes()
    decoded = data.decode("utf-8", errors="replace")
    pages = decoded.split("\f")
    if pages[-1] == "":
        pages.pop()
    missing = sum(not page.strip() for page in pages)
    if not decoded.strip():
        text_path.unlink()
        return {"status": "needs_ocr", "extractor": extractor, "pages": len(pages)}
    return {"status": "partial_text_unreviewed" if missing else "extracted_unreviewed", "extractor": extractor,
            "pages": len(pages), "pages_without_text": missing,
            "sha256": sha(data), "bytes": len(data), "path": text_path.name}


def collect_documents(catalogue, target, baseline=None, fetch=fetch_pdf, extract=extract_pdf):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path(catalogue).read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("documents"), list):
        raise ValueError("unsupported document catalogue")
    ids = [item.get("id", "") for item in catalog["documents"]]
    if any(not ID.fullmatch(item) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("document IDs must be safe and unique")
    previous = {}
    if baseline and Path(baseline).is_file():
        previous = {item["id"]: item for item in json.loads(Path(baseline).read_text(encoding="utf-8")).get("documents", [])}
    report = {"schema_version": 1, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "documents": []}
    for doc in catalog["documents"]:
        item = {k: doc[k] for k in ("id", "title", "kind", "jurisdiction", "url", "catalogue_url")}
        item["review_status"] = "unreviewed"
        raw_path = target / (item["id"] + ".pdf")
        text_path = target / (item["id"] + ".txt")
        raw_path.unlink(missing_ok=True)
        text_path.unlink(missing_ok=True)
        try:
            if not official(item["url"]) or not official(item["catalogue_url"]):
                raise ValueError("document and catalogue URLs must use official HTTPS")
            data, final_url, content_type = fetch(item["url"])
            if not official(final_url) or len(data) > MAX_SOURCE_BYTES or b"%PDF-" not in data[:1024]:
                raise ValueError("invalid or untrusted PDF response")
            raw_path.write_bytes(data)
            item.update(status="collected", final_url=final_url, content_type=content_type,
                        sha256=sha(data), bytes=len(data), path=raw_path.name)
            item["text"] = extract(raw_path, text_path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            item.update(status="unavailable", error=str(exc))
        prior = previous.get(item["id"])
        item["change"] = ("unavailable" if item["status"] != "collected" else
                          "first_observation" if not prior or prior.get("status") != "collected" else
                          "changed" if prior.get("sha256") != item.get("sha256") else "unchanged")
        report["documents"].append(item)
    (target / "documents.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Collected {sum(x['status'] == 'collected' for x in report['documents'])}/{len(report['documents'])} official documents; all require review.")
    return report


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("discover")
    scan.add_argument("--observations", required=True)
    scan.add_argument("--source-dir", required=True)
    scan.add_argument("--output", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--catalogue", default=str(ROOT / "catalog/documents.json"))
    collect.add_argument("--output", default=str(ROOT / "staging/documents"))
    collect.add_argument("--baseline")
    args = parser.parse_args()
    if args.command == "discover":
        discover(args.observations, args.source_dir, args.output)
    else:
        collect_documents(args.catalogue, args.output, args.baseline)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(f"document ingestion: {error}", file=sys.stderr)
        sys.exit(1)
