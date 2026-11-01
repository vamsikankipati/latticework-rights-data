# Rights data publisher

This directory is the initial source tree for `vamsikankipati/latticework-rights-data`. Copy its contents to the root of that repository. It has no reviewed legal entries initially. Source observations and a successful HTTP request do not establish that an act is current or complete.

## Coverage and collection

`catalog/sources.json` lists initial national source landing pages and every state/UT whose source inventory remains to be audited. Run `python publisher.py collect` to capture bounded official-page snapshots and `staging/observations.json`. The daily workflow uploads the same material as an artifact; failed sources remain marked unavailable. Inspect legal notices, gazettes, and source terms manually. This initial collector does not enumerate all legislation, parse PDFs, detect amendments, build judgment packs, or claim complete state coverage.

## Review before publication

Add UTF-8 JSON objects, one per line, to `data/entries.jsonl`. Each object requires these nonempty strings: `id`, `title`, `category`, `summary`, `content`, `citation`, `source_url`, `jurisdiction`, `verified_at` (ISO date), and `status` (`verified` or `historical`). URLs must be official government HTTPS URLs. The content and any explanation must be checked against the cited official source and applicable law before review.

Run `python publisher.py review-digests`. An authorised reviewer records each printed content digest in `data/approvals.json`, keyed by ID, e.g. `{"id": {"sha256": "...", "reviewer": "Reviewer name", "approved_at": "2026-11-01"}}`. A change to any published field invalidates its approval. Review the whole change, and protect the repository's main branch and the `rights-release` environment with required reviewers. An empty corpus or missing approval fails the build.

## Signing and distribution setup

Generate an RSA key on a trusted machine, outside the repository, using `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out rights-signing-key.pem`. Export the public key with `openssl pkey -in rights-signing-key.pem -pubout -out rights-public-key.pem`. Keep the private key secret, add its full PEM text as the GitHub Actions environment secret `RIGHTS_SIGNING_KEY`, and configure the app to pin the matching public key before enabling sync. Never commit private keys. Key replacement requires a trusted app update.

Enable GitHub Pages with GitHub Actions as the source and configure the `rights-release` environment with required reviewers. Dispatch the release workflow with a unique safe tag after review. The workflow builds `india-all.sqlite.gz`, signs the exact bytes of `manifest.json` using RSA/SHA-256, and publishes the detached binary `manifest.sig` and pack to a GitHub Release; the manifest and signature also go to Pages.

Manifest v1 has `schema_version`, `version`, `generated_at`, and `packs`. Pack records have `id`, `version`, `url`, compressed-file `sha256` and `size_bytes`, `uncompressed_size_bytes`, `jurisdiction`, `source_checked_at`, and `format`. `source_checked_at` is conservatively the earliest entry verification date; it is not a completeness guarantee. A release is immutable: use a new tag and version for changes. Pack schema is `rights_entries` with the ten fields above and SQLite `user_version=1`. The `rights_trigrams` table contains distinct Unicode NFKC-lowercased trigrams for each entry and allows indexed substring candidate searches.

The release gate does not provision accounts, keys, branch protection, source permissions, or legal reviewers. The source corpus and judgment archive need substantial additional collection and review before any claim of nationwide coverage.
