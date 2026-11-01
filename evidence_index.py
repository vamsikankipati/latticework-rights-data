#!/usr/bin/env python3
"""Build and query a local, explicitly unreviewed index of extracted source pages."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import unicodedata
from pathlib import Path


def normalized(value):
    return unicodedata.normalize("NFKC", value).lower()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def build(report_path, output):
    report_path, output = Path(report_path), Path(output)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1:
        raise ValueError("Unsupported document report")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    page_count = 0
    try:
        db.executescript("""
          CREATE TABLE pages(id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, title TEXT NOT NULL,
            source_url TEXT NOT NULL, source_sha256 TEXT NOT NULL, extracted_sha256 TEXT NOT NULL,
            page_number INTEGER NOT NULL, text TEXT NOT NULL, normalized_text TEXT NOT NULL,
            review_status TEXT NOT NULL, UNIQUE(document_id,page_number));
          CREATE TABLE trigrams(gram TEXT NOT NULL, page_id INTEGER NOT NULL,
            PRIMARY KEY(gram,page_id)) WITHOUT ROWID;
        """)
        for doc in report.get("documents", []):
            text = doc.get("text", {})
            if doc.get("status") != "collected" or text.get("status") not in ("extracted_unreviewed", "partial_text_unreviewed"):
                continue
            doc_id = doc["id"]
            if text.get("path") != doc_id + ".txt" or doc.get("path") != doc_id + ".pdf" or Path(doc_id).name != doc_id:
                raise ValueError("Unsafe evidence path")
            if digest((report_path.parent / doc["path"]).read_bytes()) != doc.get("sha256"):
                raise ValueError("Source PDF hash does not match the report")
            data = (report_path.parent / text["path"]).read_bytes()
            if digest(data) != text.get("sha256"):
                raise ValueError("Extracted evidence hash does not match the report")
            pages = data.decode("utf-8").split("\f")
            if pages and not pages[-1]:
                pages.pop()
            if len(pages) != text.get("pages"):
                raise ValueError("Extracted evidence page count does not match the report")
            for number, page in enumerate(pages, 1):
                if not page.strip():
                    continue
                folded = normalized(page)
                cursor = db.execute("INSERT INTO pages(document_id,title,source_url,source_sha256,extracted_sha256,page_number,text,normalized_text,review_status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (doc_id, doc["title"], doc["url"], doc["sha256"], text["sha256"], number, page, folded, "unreviewed"))
                grams = {folded[i:i + 3] for i in range(len(folded) - 2)}
                db.executemany("INSERT INTO trigrams(gram,page_id) VALUES(?,?)", ((gram, cursor.lastrowid) for gram in grams))
                page_count += 1
        db.commit()
        db.close()
        os.replace(temporary, output)
    except Exception:
        db.close()
        temporary.unlink(missing_ok=True)
        raise
    print(f"Indexed {page_count} unreviewed source pages; nothing was published.")
    return page_count


def search(database, query, limit=20):
    query = normalized(query.strip())
    if not query:
        raise ValueError("A search phrase is required")
    with closing(sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)) as db:
        grams = {query[i:i + 3] for i in range(len(query) - 2)}
        if grams:
            gram = min(grams, key=lambda g: db.execute("SELECT count(*) FROM trigrams WHERE gram=?", (g,)).fetchone()[0])
            sql = "SELECT document_id,title,source_url,page_number,text FROM pages WHERE id IN (SELECT page_id FROM trigrams WHERE gram=?) AND instr(normalized_text,?) > 0 ORDER BY document_id,page_number LIMIT ?"
            rows = db.execute(sql, (gram, query, max(1, min(limit, 100)))).fetchall()
        else:
            rows = db.execute("SELECT document_id,title,source_url,page_number,text FROM pages WHERE instr(normalized_text,?) > 0 ORDER BY document_id,page_number LIMIT ?", (query, max(1, min(limit, 100)))).fetchall()
    return [dict(zip(("document_id", "title", "source_url", "page_number", "text"), row), review_status="unreviewed") for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("report")
    create.add_argument("output")
    lookup = commands.add_parser("search")
    lookup.add_argument("database")
    lookup.add_argument("query")
    lookup.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.command == "build":
        build(args.report, args.output)
    else:
        print(json.dumps(search(args.database, args.query, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
