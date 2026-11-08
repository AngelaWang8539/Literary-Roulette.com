import io
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from tools.sync_gutenberg_rdf import parse_archive, parse_rdf_record
from gutenberg_pool import GutendexPool

RDF_NS = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
DCT_NS = 'http://purl.org/dc/terms/'
PG_NS = 'http://www.gutenberg.org/2009/pgterms/'


def fixture(gid=1342, language='en', rights='Public domain in the USA.', downloads=208430, title='Pride and Prejudice'):
    return f'''<?xml version="1.0" encoding="utf-8"?>
    <rdf:RDF xmlns:rdf="{RDF_NS}" xmlns:dcterms="{DCT_NS}" xmlns:pgterms="{PG_NS}">
      <pgterms:ebook rdf:about="ebooks/{gid}">
        <dcterms:title>{title}</dcterms:title>
        <dcterms:creator><pgterms:agent><pgterms:name>Austen, Jane</pgterms:name></pgterms:agent></dcterms:creator>
        <dcterms:language><rdf:Description><rdf:value>{language}</rdf:value></rdf:Description></dcterms:language>
        <dcterms:rights>{rights}</dcterms:rights>
        <pgterms:downloads>{downloads}</pgterms:downloads>
        <dcterms:subject><rdf:Description><rdf:value>England -- Fiction</rdf:value></rdf:Description></dcterms:subject>
        <pgterms:bookshelf><rdf:Description><rdf:value>Best Books Ever Listings</rdf:value></rdf:Description></pgterms:bookshelf>
        <dcterms:hasFormat><pgterms:file rdf:about="https://www.gutenberg.org/ebooks/{gid}.txt.utf-8">
          <dcterms:format><rdf:Description><rdf:value>text/plain; charset=utf-8</rdf:value></rdf:Description></dcterms:format>
        </pgterms:file></dcterms:hasFormat>
      </pgterms:ebook>
    </rdf:RDF>'''.encode('utf-8')


class TextCacheLimitTests(unittest.TestCase):
    def test_disk_cache_is_pruned_by_age_file_count_and_total_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "cache" / "en"
            cache.mkdir(parents=True)
            old = cache / "pg1.txt"
            recent_a = cache / "pg2.txt"
            recent_b = cache / "pg3.txt"
            old.write_text("old!", encoding="utf-8")
            recent_a.write_text("one!", encoding="utf-8")
            recent_b.write_text("two!", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 7200, now - 7200))
            os.utime(recent_a, (now - 20, now - 20))
            os.utime(recent_b, (now - 10, now - 10))

            pool = GutendexPool(
                root / "missing-catalogue.json", root / "cache",
                max_cache_files=2, max_cache_bytes=8, cache_ttl_seconds=3600,
            )
            self.assertFalse(old.exists())  # expired files are removed
            self.assertTrue(recent_a.exists())
            self.assertTrue(recent_b.exists())
            self.assertEqual(sum(p.stat().st_size for p in cache.glob("pg*.txt")), 8)


class RDFImportTests(unittest.TestCase):
    def test_converts_one_record_to_gutendex_schema(self):
        row = parse_rdf_record(fixture(), 'pg1342.rdf')
        self.assertEqual(row['id'], 1342)
        self.assertEqual(row['title'], 'Pride and Prejudice')
        self.assertEqual(row['authors'], [{'name': 'Austen, Jane'}])
        self.assertEqual(row['languages'], ['en'])
        self.assertEqual(row['download_count'], 208430)
        self.assertIs(row['copyright'], False)
        self.assertTrue(any(k.startswith('text/plain') for k in row['formats']))
        self.assertEqual(row['subjects'], ['England -- Fiction'])

    def test_recognises_french_language_label(self):
        row = parse_rdf_record(fixture(gid=27046, language='Français', title='Le Projet Gutenberg'), 'pg27046.rdf')
        self.assertEqual(row['languages'], ['fr'])

    def test_recognises_language_subtags(self):
        row = parse_rdf_record(fixture(gid=24264, language='zh-Hant', title='紅樓夢'), 'pg24264.rdf')
        self.assertEqual(row['languages'], ['zh'])

    def test_excludes_copyrighted_records(self):
        row = parse_rdf_record(fixture(rights='Copyrighted. Read the copyright notice inside this book.'), 'pg1342.rdf')
        self.assertIsNone(row)

    def test_does_not_mistake_zip_container_for_plain_text(self):
        xml = fixture().replace(b'https://www.gutenberg.org/ebooks/1342.txt.utf-8',
                                b'https://www.gutenberg.org/files/1342/1342-0.zip')
        self.assertIsNone(parse_rdf_record(xml, 'pg1342.rdf'))

    def test_archive_groups_records_by_language(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / 'test.tar.bz2'
            with tarfile.open(archive_path, 'w:bz2') as archive:
                for gid, xml in ((1342, fixture()), (27046, fixture(27046, 'French', title='French title')), (24264, fixture(24264, 'Chinese', title='中文'))):
                    info = tarfile.TarInfo(f'cache/epub/{gid}/pg{gid}.rdf')
                    info.size = len(xml)
                    archive.addfile(info, io.BytesIO(xml))
            rows = parse_archive(archive_path)
            self.assertEqual(len(rows['en']), 1)
            self.assertEqual(len(rows['fr']), 1)
            self.assertEqual(len(rows['zh']), 1)
            self.assertEqual(rows['zh'][0]['id'], 24264)


if __name__ == '__main__':
    unittest.main()
