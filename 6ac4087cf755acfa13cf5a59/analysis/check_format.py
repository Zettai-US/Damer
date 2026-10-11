#!/usr/bin/env python3
"""Check the manuscript's requested OSDI layout and generated PDF.

This checks format, not novelty, empirical adequacy, authorship, or acceptance.
Requires a built main.pdf, its LaTeX log/aux files, and Poppler utilities.
"""
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main():
    log = (ROOT/'main.log').read_text()
    aux = (ROOT/'main.aux').read_text()
    compact_log = log.replace('\n', '')
    expected = {'textwidth': 505.89, 'textheight': 650.43,
                'columnsep': 23.84923, 'baseline': 12.0, 'font': 10.0}
    for name, value in expected.items():
        match = re.search(rf'{name}=([\d.]+)pt', compact_log)
        if not match or abs(float(match[1])-value) > .02:
            raise SystemExit(f'Unexpected layout value for {name}: {match}')
    refs = re.search(r'\\newlabel\{page:references\}\{\{[^}]*\}\{(\d+)\}', aux)
    if not refs or int(refs[1])-1 != 12:
        raise SystemExit(f'Expected 12 body pages, reference label is {refs}')
    info = run('pdfinfo', 'main.pdf')
    if not re.search(r'Page size:\s+612 x 792 pts', info):
        raise SystemExit('PDF page size is not US Letter')
    if 'Overfull' in log or 'undefined' in log.lower():
        raise SystemExit('LaTeX contains overfull boxes or unresolved references')
    font_lines = run('pdffonts', 'main.pdf').splitlines()[2:]
    for line in font_lines:
        # Last columns are emb, sub, uni, object ID, generation ID.
        if line.split()[-5] != 'yes':
            raise SystemExit(f'Unembedded font: {line}')
    bbox = run('pdftotext', '-bbox', 'main.pdf', '-')
    # Poppler can emit control-code text for math glyphs that XML 1.0 rejects.
    bbox = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', bbox)
    xml = ET.fromstring(bbox)
    ns = {'x': 'http://www.w3.org/1999/xhtml'}
    excursions = []
    for number, page in enumerate(xml.findall('.//x:page', ns), 1):
        for word in page.findall('.//x:word', ns):
            x0, y0, x1, y1 = [float(word.attrib[k]) for k in
                              ['xMin', 'yMin', 'xMax', 'yMax']]
            text = ''.join(word.itertext())
            if text.isdigit() and y0 > 730:  # Numbered footer is outside body.
                continue
            # TeX's block is checked above. Font descenders and the official
            # template's microtype protrusion can extend ink by about 2 points.
            if x0 < 51 or x1 > 561 or y0 < 69 or y1 > 723:
                excursions.append((number, text, (x0, y0, x1, y1)))
    if excursions:
        raise SystemExit(f'Text outside expected body block: {excursions[:10]}')
    pages = re.search(r'Pages:\s+(\d+)', info)[1]
    print(f'PASS: 12 body pages; {pages} total pages including references.')
    print('PASS: Letter, 7 x 9 in text block, 0.33 in gutter, 10/12 pt type.')
    print('PASS: embedded fonts, resolved references, no overfull boxes.')
    print('PASS: ink bounds match template (3 pt font tolerance; footers excluded).')


if __name__ == '__main__':
    main()
