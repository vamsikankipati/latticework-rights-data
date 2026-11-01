#!/usr/bin/env python3
"""Collect source observations and build reviewed Rights SQLite packs."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIELDS = ("id", "title", "category", "summary", "content", "citation", "source_url", "jurisdiction", "verified_at", "status")
MAX_SOURCE_BYTES = 10 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_entry(entry):
    return json.dumps({key: entry[key] for key in FIELDS}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def official(url):
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host.endswith(".gov.in") or host.endswith(".nic.in")) and parsed.username is None and parsed.password is None


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official(newurl):
            raise ValueError("redirect left official government domains")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def entries(path):
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in FIELDS):
            raise ValueError(f"line {line_no}: all entry fields must be non-empty strings")
        if not official(row["source_url"]):
            raise ValueError(f"line {line_no}: source must be an official HTTPS URL")
        if row["status"] not in ("verified", "historical"):
            raise ValueError(f"line {line_no}: status must be verified or historical")
        date = dt.date.fromisoformat(row["verified_at"])
        if date > dt.datetime.now(dt.timezone.utc).date():
            raise ValueError(f"line {line_no}: verification date is in the future")
        result.append({key: row[key] for key in FIELDS})
    if len({r["id"] for r in result}) != len(result):
        raise ValueError("duplicate entry ID")
    return result


def check_approvals(rows, path):
    approvals = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        raise ValueError("cannot publish an empty or unreviewed corpus")
    for row in rows:
        approval = approvals.get(row["id"], {})
        if approval.get("sha256") != sha(canonical_entry(row)) or not approval.get("reviewer", "").strip():
            raise ValueError(f"entry {row['id']} needs an approval matching its exact content")
        approved_at = dt.date.fromisoformat(approval.get("approved_at", ""))
        if approved_at > dt.datetime.now(dt.timezone.utc).date():
            raise ValueError(f"entry {row['id']} has a future approval date")


def collect(args):
    catalogue = json.loads((ROOT / "catalog/sources.json").read_text(encoding="utf-8"))
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    previous = {}
    if args.baseline and Path(args.baseline).is_file():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        previous = {item["id"]: item for item in baseline.get("sources", [])}
    report = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sources": [], "jurisdictions_pending_source_audit": catalogue["jurisdictions_pending_source_audit"]}
    for source in catalogue["sources"]:
        item = {"id": source["id"], "url": source["url"], "jurisdiction": source["jurisdiction"]}
        try:
            if not official(source["url"]):
                raise ValueError("source URL is not official HTTPS")
            request = urllib.request.Request(source["url"], headers={"User-Agent": "LatticeworkRightsCollector/1.0 (public legal source audit)"})
            with urllib.request.build_opener(OfficialRedirect()).open(request, timeout=30) as response:
                if not official(response.geturl()):
                    raise ValueError("redirect left official government domains")
                data = response.read(MAX_SOURCE_BYTES + 1)
                if len(data) > MAX_SOURCE_BYTES:
                    raise ValueError("source exceeds collection size limit")
                item.update(status="observed", http_status=response.status, final_url=response.geturl(), sha256=sha(data), bytes=len(data), content_type=response.headers.get("Content-Type", ""))
                (target / (source["id"] + ".snapshot")).write_bytes(data)
        except Exception as exc:
            item.update(status="unavailable", error=str(exc))
        prior = previous.get(source["id"])
        item["change"] = ("unavailable" if item["status"] == "unavailable" else
                          "first_observation" if not prior or prior.get("status") != "observed" else
                          "changed" if prior.get("sha256") != item.get("sha256") else "unchanged")
        report["sources"].append(item)
    (target / "observations.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Recorded {len(report['sources'])} sources; {sum(x['status'] == 'unavailable' for x in report['sources'])} unavailable. These observations are not reviewed legal content.")


def build(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.version):
        raise ValueError("version must be a safe release tag")
    rows = entries(ROOT / "data/entries.jsonl")
    check_approvals(rows, ROOT / "data/approvals.json")
    subprocess.run(["openssl", "rsa", "-in", args.key, "-check", "-noout"], check=True, stdout=subprocess.DEVNULL)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    dbfile = out / "india-all.sqlite"
    if dbfile.exists():
        dbfile.unlink()
    db = sqlite3.connect(dbfile)
    try:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE rights_entries (id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL, citation TEXT NOT NULL, source_url TEXT NOT NULL, jurisdiction TEXT NOT NULL, verified_at TEXT NOT NULL, status TEXT NOT NULL)")
        db.executemany("INSERT INTO rights_entries VALUES (?,?,?,?,?,?,?,?,?,?)", [tuple(row[k] for k in FIELDS) for row in sorted(rows, key=lambda r: r["id"])])
        db.execute("CREATE TABLE rights_trigrams (gram TEXT NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY (gram, entry_id)) WITHOUT ROWID")
        for row in rows:
            grams = set()
            for field in ("title", "category", "summary", "content", "citation", "jurisdiction"):
                normal = unicodedata.normalize("NFKC", row[field]).lower()
                grams.update(normal[index:index + 3] for index in range(max(0, len(normal) - 2)))
            db.executemany("INSERT INTO rights_trigrams VALUES (?,?)", ((gram, row["id"]) for gram in grams))
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    finally:
        db.close()
    compressed = out / "india-all.sqlite.gz"
    with dbfile.open("rb") as source, compressed.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as zipped:
            shutil.copyfileobj(source, zipped, length=1024 * 1024)
    if compressed.stat().st_size >= 2 * 1024 * 1024 * 1024:
        raise ValueError("pack exceeds GitHub's per-asset limit; split it before publishing")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest = {"schema_version": 1, "version": args.version, "generated_at": now, "packs": [{"id": "india-all", "version": args.version, "url": f"https://github.com/vamsikankipati/latticework-rights-data/releases/download/{args.version}/india-all.sqlite.gz", "sha256": file_sha(compressed), "size_bytes": compressed.stat().st_size, "uncompressed_size_bytes": dbfile.stat().st_size, "jurisdiction": "IN", "source_checked_at": min(r["verified_at"] for r in rows), "format": "sqlite-gzip-v1"}]}
    (out / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", args.key, "-out", str(out / "manifest.sig"), str(out / "manifest.json")], check=True)
    print(f"Built signed pack with {len(rows)} approved entries")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("collect")
    scan.add_argument("--output", default=str(ROOT / "staging"))
    scan.add_argument("--baseline", help="Prior observations.json for change detection")
    pack = sub.add_parser("build")
    pack.add_argument("--version", required=True)
    pack.add_argument("--key", required=True)
    pack.add_argument("--output", default=str(ROOT / "dist"))
    digest = sub.add_parser("review-digests")
    digest.add_argument("--input", default=str(ROOT / "data/entries.jsonl"))
    args = parser.parse_args()
    if args.command == "collect":
        collect(args)
    elif args.command == "build":
        build(args)
    else:
        for row in entries(Path(args.input)):
            print(row["id"], sha(canonical_entry(row)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"publisher: {error}", file=sys.stderr)
        sys.exit(1)
