"""Creates sample files for the Node tests: a Gutenberg-style text, EPUBs (good, DRM, damaged) and the expected Python output."""
import json
import random
import zipfile
from pathlib import Path

import textproc
from tests.fixtures import fake_gutenberg, paragraph

CONTAINER = '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'


def xhtml(body):
    return f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title><style>p{{}}</style></head><body>{body}</body></html>'


def make_epub(path, encryption=None, rights=False):
    rng = random.Random(11)
    chapters = []
    for c in range(1, 4):
        paras = "".join(f"<p>{paragraph(rng).replace('&', '&amp;')} Caf&#233; &amp; &#8220;quoted&#8221;.</p>" for _ in range(14))
        chapters.append((f"ch{c}.xhtml", xhtml(f"<h2>Chapter {c}</h2>{paras}")))
    pages = [("cover.xhtml", xhtml("<p>COVER PAGE TEXT</p>")), ("nav.xhtml", xhtml("<nav><ol><li>NAV LIST</li></ol></nav>")),
             ("titlepage.xhtml", xhtml("<h1>Title Page</h1><p>TITLE PAGE TEXT</p>")), *chapters,
             ("copyright.xhtml", xhtml("<p>COPYRIGHT NOTICE TEXT</p>"))]
    manifest = "".join(f'<item id="i{i}" href="{n}" media-type="application/xhtml+xml"{" properties=\"nav\"" if n == "nav.xhtml" else ""}/>' for i, (n, _) in enumerate(pages))
    spine = "".join(f'<itemref idref="i{i}"/>' for i in range(len(pages)))
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>The Fixture &amp; Friends</dc:title><dc:creator>Una Uploader</dc:creator></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        if encryption:
            z.writestr("META-INF/encryption.xml", f'<encryption><EncryptedData><EncryptionMethod Algorithm="{encryption}"/></EncryptedData></encryption>')
        if rights:
            z.writestr("META-INF/rights.xml", "<rights/>")
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        for name, body in pages:
            z.writestr(f"OEBPS/{name}", body, compress_type=zipfile.ZIP_DEFLATED)


def build_fixtures(out):
    out = Path(out)
    raw = fake_gutenberg("Parity Tale", "Ann Author", paragraphs=160, seed=21)
    (out / "sample.txt").write_text(raw, encoding="utf-8")
    text, _ = textproc.clean_book_text(raw)
    (out / "expected.json").write_text(json.dumps({"text": text, "passages": textproc.split_passages(text)}), encoding="utf-8")
    make_epub(out / "good.epub")
    make_epub(out / "drm.epub", encryption="http://www.w3.org/2001/04/xmlenc#aes128-cbc")
    make_epub(out / "adept.epub", rights=True)
    make_epub(out / "fonts.epub", encryption="http://www.idpf.org/2008/embedding")
    data = (out / "good.epub").read_bytes()
    (out / "truncated.epub").write_bytes(data[: len(data) // 2])
    (out / "random.epub").write_bytes(random.Random(1).randbytes(5000))
    (out / "binary.txt").write_bytes(random.Random(2).randbytes(6000))
