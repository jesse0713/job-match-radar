# -*- coding: utf-8 -*-
"""把 .docx 简历抽成纯文本，便于人工摘录关键词。

用法：
    python read_resume.py path/to/resume.docx
    python read_resume.py path/to/resume.docx > resume_text.txt

仅在本机运行，读到的内容不会被上传。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def iter_block_items(parent):
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = parent.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def dump(path: Path) -> None:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                print(f"[{block.style.name}] {text}")
        elif isinstance(block, Table):
            for row in block.rows:
                cells = [c.text.strip().replace("\n", " / ") for c in row.cells]
                print("TBL | " + " | ".join(cells))


def main() -> int:
    ap = argparse.ArgumentParser(description="把 .docx 简历抽成纯文本")
    ap.add_argument("path", help="简历文件路径（.docx）")
    args = ap.parse_args()
    src = Path(args.path).expanduser()
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 1
    dump(src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
