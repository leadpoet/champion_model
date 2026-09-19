"""Readable workbook excerpts; saved evidence and receipts remain untouched."""

import json
import re
import sys

from scrapingdog import _VisibleTextParser


MAX_EXCERPT_CHARACTERS = 2000
EXCERPT_SUFFIX = "\n[Excerpt; full text in saved receipt.]"


def source_excerpt(value):
    text = str(value or "").strip()
    if re.search(r"<(?:!DOCTYPE\b|/?[a-zA-Z][\w:-]*(?:\s[^>]*|\s*/?)>)", text):
        parser = _VisibleTextParser()
        parser.feed(text)
        parser.close()
        text = " ".join(" ".join(parser.parts).split())
        if not text:
            return "No readable page text; see source URL and saved receipt."
    if len(text) > MAX_EXCERPT_CHARACTERS:
        text = text[:MAX_EXCERPT_CHARACTERS - len(EXCERPT_SUFFIX)].rstrip() + EXCERPT_SUFFIX
    return text


if __name__ == "__main__":
    print(json.dumps([source_excerpt(value) for value in json.load(sys.stdin)]))
