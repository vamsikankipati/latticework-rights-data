import io
import importlib.util
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import document_ingest as ingest


class DocumentIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalogue = self.root / 'catalog.json'
        self.document = {
            'id': 'sample-act', 'title': 'Sample Act', 'kind': 'act_as_published',
            'jurisdiction': 'IN', 'url': 'https://example.gov.in/act.pdf',
            'catalogue_url': 'https://example.gov.in/acts',
        }
        self.catalogue.write_text(json.dumps({'schema_version': 1, 'documents': [self.document]}), encoding='utf-8')
        self.target = self.root / 'output'

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def extract(raw, text):
        text.write_text('Unreviewed text: not liable.', encoding='utf-8')
        return {'status': 'extracted_unreviewed', 'path': text.name}

    def collect(self, payload=b'%PDF-1.4\nfixture', baseline=None, final='https://example.gov.in/act.pdf'):
        def fetch(url):
            return payload, final, 'application/pdf'
        return ingest.collect_documents(self.catalogue, self.target, baseline, fetch=fetch, extract=self.extract)

    def test_collection_tracks_hashes_and_never_approves_content(self):
        first = self.collect()
        row = first['documents'][0]
        self.assertEqual(row['status'], 'collected')
        self.assertEqual(row['review_status'], 'unreviewed')
        self.assertEqual(row['text']['status'], 'extracted_unreviewed')
        self.assertEqual((self.target / row['path']).read_bytes(), b'%PDF-1.4\nfixture')
        baseline = self.root / 'baseline.json'
        baseline.write_text(json.dumps(first), encoding='utf-8')
        self.assertEqual(self.collect(baseline=baseline)['documents'][0]['change'], 'unchanged')
        changed = self.collect(b'%PDF-1.4\nnew content', baseline=baseline)['documents'][0]
        self.assertEqual(changed['change'], 'changed')
        self.assertNotEqual(changed['sha256'], row['sha256'])
        self.assertFalse((self.root / 'data' / 'entries.jsonl').exists())

    def test_rejects_html_untrusted_redirect_and_unsafe_ids(self):
        for payload, final in [(b'<html>blocked</html>', self.document['url']),
                               (b'%PDF-1.4', 'https://evil.example/act.pdf')]:
            result = self.collect(payload, final=final)['documents'][0]
            self.assertEqual(result['status'], 'unavailable')
            self.assertFalse((self.target / 'sample-act.pdf').exists())
            self.assertFalse((self.target / 'sample-act.txt').exists())
        self.document['id'] = '../outside'
        self.catalogue.write_text(json.dumps({'schema_version': 1, 'documents': [self.document]}), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.collect()

    def test_unavailable_refresh_keeps_prior_evidence(self):
        self.collect()
        pdf = (self.target / 'sample-act.pdf').read_bytes()
        text = (self.target / 'sample-act.txt').read_bytes()
        result = self.collect(b'<html>temporarily unavailable</html>')['documents'][0]
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual((self.target / 'sample-act.pdf').read_bytes(), pdf)
        self.assertEqual((self.target / 'sample-act.txt').read_bytes(), text)

    def test_discovery_only_lists_official_pdfs(self):
        source = {'id': 'consumer', 'status': 'observed', 'content_type': 'text/html; charset=utf-8',
                  'url': 'https://example.gov.in/index', 'final_url': 'https://example.gov.in/index'}
        observations = self.root / 'observations.json'
        observations.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
        (self.root / 'consumer.snapshot').write_text('''
          <a href="/one.PDF#page=1">Act <b>One</b></a>
          <a href="/one.PDF">Duplicate</a>
          <a href="https://elsewhere.nic.in/two.pdf?x=1">Another</a>
          <a href="/legal records/three.pdf">A document with spaces</a>
          <a href="https://example.gov.in.evil.test/steal.pdf">Bad</a>
          <a href="https://example.gov.in/image.png">Image</a>
        ''', encoding='utf-8')
        output = self.root / 'candidates.json'
        ingest.discover(observations, self.root, output)
        candidates = json.loads(output.read_text(encoding='utf-8'))['candidates']
        self.assertEqual(len(candidates), 3)
        self.assertIn('https://example.gov.in/legal%20records/three.pdf', [c['url'] for c in candidates])
        self.assertTrue(all(c['review_status'] == 'unreviewed_candidate' for c in candidates))
        self.assertIn('Act One', [c['label'] for c in candidates])
        self.assertFalse(list(self.root.glob('*.pdf')))

    def test_robots_rules_and_unavailable_policy(self):
        class FakeResponse(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse(b'User-agent: *\nDisallow: /private\n')
        ingest._robots.clear()
        with patch.object(ingest.urllib.request, 'build_opener', return_value=FakeOpener()):
            ingest.check_robots('https://example.gov.in/public.pdf')
            with self.assertRaisesRegex(ValueError, 'disallows'):
                ingest.check_robots('https://example.gov.in/private/file.pdf')
        class ForbiddenOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {}, None)
        ingest._robots.clear()
        with patch.object(ingest.urllib.request, 'build_opener', return_value=ForbiddenOpener()):
            with self.assertRaisesRegex(ValueError, 'robots.txt unavailable'):
                ingest.check_robots('https://example.gov.in/public.pdf')

    def test_candidate_backfill_is_bounded_resumable_and_unreviewed(self):
        candidates = self.root / 'candidates.json'
        observations = self.root / 'observations.json'
        rows = [{'id': 'candidate-' + name, 'url': f'https://example.gov.in/{name}.pdf', 'label': name, 'source_ids': ['central', 'state']} for name in ('a', 'b', 'c')]
        candidates.write_text(json.dumps({'schema_version': 1, 'candidates': rows}), encoding='utf-8')
        observations.write_text(json.dumps({'sources': [
            {'id': 'central', 'status': 'observed', 'url': 'https://example.gov.in/acts', 'jurisdiction': 'IN'},
            {'id': 'state', 'status': 'observed', 'url': 'https://example.gov.in/state', 'jurisdiction': 'IN-KA'},
        ]}), encoding='utf-8')
        state = self.root / 'state.json'
        def fetch(url): return b'%PDF-1.4 fixture', url, 'application/pdf'
        with patch.object(ingest.time, 'sleep'):
            first = ingest.collect_candidates(candidates, observations, self.target, state, 1, fetch, self.extract)
            second = ingest.collect_candidates(candidates, observations, self.target, state, 1, fetch, self.extract)
        self.assertEqual(first['documents'][0]['id'], 'candidate-a')
        self.assertEqual(second['documents'][0]['id'], 'candidate-b')
        self.assertEqual(first['documents'][0]['jurisdiction'], 'UNKNOWN')
        self.assertEqual(first['documents'][0]['review_status'], 'unreviewed')
        self.assertEqual(len(json.loads(state.read_text(encoding='utf-8'))['items']), 2)
        self.assertEqual(len(json.loads((self.target / 'all-documents.json').read_text(encoding='utf-8'))['documents']), 2)

    def test_candidate_backfill_recovers_hash_only_state_without_refetching(self):
        candidates = self.root / 'candidates.json'
        observations = self.root / 'observations.json'
        candidate = {'id': 'candidate-legacy', 'url': 'https://example.gov.in/legacy.pdf', 'label': 'Legacy Act', 'source_ids': ['central']}
        candidates.write_text(json.dumps({'schema_version': 1, 'candidates': [candidate]}), encoding='utf-8')
        observations.write_text(json.dumps({'sources': [
            {'id': 'central', 'status': 'observed', 'url': 'https://example.gov.in/acts', 'jurisdiction': 'IN'},
        ]}), encoding='utf-8')
        self.target.mkdir(parents=True)
        pdf = b'%PDF-1.4 legacy fixture'
        (self.target / 'candidate-legacy.pdf').write_bytes(pdf)
        (self.target / 'candidate-legacy.txt').write_text('Citizen rights reference.', encoding='utf-8')
        today = ingest.dt.datetime.now(ingest.dt.timezone.utc).date().isoformat()
        state = self.root / 'state.json'
        state.write_text(json.dumps({'schema_version': 1, 'items': {'candidate-legacy': {
            'status': 'collected', 'sha256': ingest.sha(pdf), 'last_attempt': today,
        }}}), encoding='utf-8')

        def fail_fetch(_url):
            self.fail('recovered candidates should not be fetched again in this run')

        result = ingest.collect_candidates(candidates, observations, self.target, state, 1, fail_fetch, self.extract)
        recovered = json.loads(state.read_text(encoding='utf-8'))['items']['candidate-legacy']
        aggregate = json.loads((self.target / 'all-documents.json').read_text(encoding='utf-8'))['documents']
        self.assertEqual(result['documents'], [])
        self.assertEqual(recovered['review_status'], 'unreviewed')
        self.assertEqual(recovered['text']['status'], 'extracted_unreviewed')
        self.assertEqual(recovered['text']['extractor'], 'recovered')
        self.assertEqual(len(aggregate), 1)

    def test_extraction_reports_blank_pages(self):
        text = self.root / 'text.txt'
        text.write_text('First page\f\fThird page\f', encoding='utf-8')
        result = ingest.describe_text(text, 'fixture')
        self.assertEqual(result['status'], 'partial_text_unreviewed')
        self.assertEqual(result['pages'], 3)
        self.assertEqual(result['pages_without_text'], 1)
        text.write_text('\f\f', encoding='utf-8')
        self.assertEqual(ingest.describe_text(text, 'fixture')['status'], 'needs_ocr')
        self.assertFalse(text.exists())

    def test_real_pdf_extraction_when_available(self):
        if not shutil.which('pdftotext') and not importlib.util.find_spec('pypdfium2'):
            self.skipTest('Neither Poppler nor PDFium is available')
        stream = b'BT /F1 12 Tf 10 200 Td (Not liable.) Tj ET\n'
        objects = [
            b'<< /Type /Catalog /Pages 2 0 R >>',
            b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
            f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'endstream',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        ]
        pdf = bytearray(b'%PDF-1.4\n')
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
        xref = len(pdf)
        pdf.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
        for offset in offsets[1:]:
            pdf.extend(f'{offset:010d} 00000 n \n'.encode())
        pdf.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
        raw, text = self.root / 'test.pdf', self.root / 'test.txt'
        raw.write_bytes(pdf)
        result = ingest.extract_pdf(raw, text)
        self.assertEqual(result['status'], 'extracted_unreviewed', result)
        self.assertIn('Not liable.', text.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
