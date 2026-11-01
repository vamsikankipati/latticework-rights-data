# Rights data publisher

This directory is the initial source tree for `vamsikankipati/latticework-rights-data`. Copy its contents to the root of that repository. It has no reviewed legal entries initially. Source observations and a successful HTTP request do not establish that an act is current or complete.

## Coverage and collection

`catalog/sources.json` lists national and state/UT source landing pages. Run `python publisher.py collect` to capture bounded official-page snapshots and `staging/observations.json`. The daily workflow uploads only metadata and candidate reports; failed sources remain marked unavailable. Source snapshots, PDFs, and extracted text stay in local or runner staging and are not uploaded as workflow artifacts. Inspect legal notices, gazettes, and source terms manually. This collector does not enumerate all legislation, consolidate amendments, perform OCR, build judgment packs, or claim complete state coverage.

The document pipeline also inventories official PDF links from collected HTML and downloads a small, explicit list in `catalog/documents.json`. `python document_ingest.py discover --observations staging/observations.json --source-dir staging --output staging/document-candidates.json` creates an **unreviewed candidate list**; it does not download or publish candidates. `python document_ingest.py collect --output staging/documents` downloads the registered PDFs, records hashes and extraction status, and extracts text using Poppler's `pdftotext` or the optional `pypdfium2` Python package. The collector respects `robots.txt`, limits response sizes, and rejects non-government redirects and non-PDF responses. Raw PDFs and extracted text remain unreviewed evidence: extraction may omit text, layout, or scanned pages, and an as-enacted Act may have been amended. A reviewer must check the original documents, amendments, commencement, coverage, redistribution terms, and applicability before creating entries and approval digests. Source and document observations do not determine whether a provision is currently in force.

For local review, `python evidence_index.py build staging/documents/documents.json staging/evidence.sqlite` indexes extracted pages with source URLs, document hashes and page numbers. Search with `python evidence_index.py search staging/evidence.sqlite "not liable"`. Search is case-insensitive and supports partial words and phrases. This index is unreviewed evidence, remains in ignored staging, and never produces approved entries or release packs.

## Review before publication

Add UTF-8 JSON objects, one per line, to `data/entries.jsonl`. Each object requires these nonempty strings: `id`, `title`, `category`, `summary`, `content`, `citation`, `source_url`, `jurisdiction`, `verified_at` (ISO date), and `status` (`verified` or `historical`). URLs must be official government HTTPS URLs. The content and any explanation must be checked against the cited official source and applicable law before review.

Run `python publisher.py review-digests`. An authorised reviewer records each printed content digest in `data/approvals.json`, keyed by ID, e.g. `{"id": {"sha256": "...", "reviewer": "Reviewer name", "approved_at": "2026-11-01"}}`. A change to any published field invalidates its approval. Review the whole change, and protect the repository's main branch and the `rights-release` environment with required reviewers. An empty corpus or missing approval fails the build.

## Signing and distribution setup

Generate an RSA key on a trusted machine, outside the repository, using `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out rights-signing-key.pem`. Export the public key with `openssl pkey -in rights-signing-key.pem -pubout -out rights-public-key.pem`. Keep the private key secret, add its full PEM text as the GitHub Actions environment secret `RIGHTS_SIGNING_KEY`, and configure the app to pin the matching public key before enabling sync. Never commit private keys. Key replacement requires a trusted app update.

Enable GitHub Pages with GitHub Actions as the source and configure the `rights-release` environment with required reviewers. Dispatch the release workflow with a unique safe tag after review. The workflow builds `india-all.sqlite.gz`, signs the exact bytes of `manifest.json` using RSA/SHA-256, and publishes the detached binary `manifest.sig` and pack to a GitHub Release; the manifest and signature also go to Pages.

Manifest v1 has `schema_version`, `version`, `generated_at`, and `packs`. Pack records have `id`, `version`, `url`, compressed-file `sha256` and `size_bytes`, `uncompressed_size_bytes`, `jurisdiction`, `source_checked_at`, and `format`. `source_checked_at` is conservatively the earliest entry verification date; it is not a completeness guarantee. A release is immutable: use a new tag and version for changes. Pack schema is `rights_entries` with the ten fields above and SQLite `user_version=1`. The `rights_trigrams` table contains distinct Unicode NFKC-lowercased trigrams for each entry and allows indexed substring candidate searches.

The release gate does not provision accounts, keys, branch protection, source permissions, or legal reviewers. The source corpus and judgment archive need substantial additional collection and review before any claim of nationwide coverage.
