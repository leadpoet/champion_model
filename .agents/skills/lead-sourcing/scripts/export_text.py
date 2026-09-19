"""Readable workbook excerpts; saved evidence and receipts remain untouched."""

import json
import re
import sys

from scrapingdog import _VisibleTextParser


MAX_EXCERPT_CHARACTERS = 2000
EXCERPT_SUFFIX = "\n[Excerpt; full text in saved receipt.]"


def _readable_markdown(text):
    # Only presentation syntax: keep literal code and URLs byte-for-byte.
    parts = re.split(r"(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`)", text)
    for index in range(0, len(parts), 2):
        part = re.sub(r"(?m)^ {0,3}#{1,6}(?:[ \t]+|(?=\n|$))", "", parts[index])
        part = re.sub(r"(?<!!)\[([^\]\n]+)\]\((https?://[^\s<>]+)\)", r"\1 (\2)", part)
        tokens = re.split(r"(https?://[^\s<>]+)", part)
        for offset in range(0, len(tokens), 2):
            tokens[offset] = re.sub(r"(?<![\\*])\*\*(?=\S)([^\n]+?)(?<=\S)\*\*(?!\*)", r"\1", tokens[offset])
        parts[index] = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(tokens))
    return "".join(parts).strip()


def source_excerpt(value):
    text = str(value or "").strip()
    if re.search(r"<(?:!DOCTYPE\b|/?[a-zA-Z][\w:-]*(?:\s[^>]*|\s*/?)>)", text):
        parser = _VisibleTextParser()
        parser.feed(text)
        parser.close()
        text = " ".join(" ".join(parser.parts).split())
        if not text:
            return "No readable page text; see source URL and saved receipt."
    text = _readable_markdown(text)
    if len(text) > MAX_EXCERPT_CHARACTERS:
        text = text[:MAX_EXCERPT_CHARACTERS - len(EXCERPT_SUFFIX)].rstrip() + EXCERPT_SUFFIX
    return text


if __name__ == "__main__":
    print(json.dumps([source_excerpt(value) for value in json.load(sys.stdin)]))
