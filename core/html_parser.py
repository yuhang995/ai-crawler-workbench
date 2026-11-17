"""Keep browser-valid event attributes out of lxml's text recovery path.

Older libxml2 HTML parsers treat Alpine's @event attributes as malformed and
can terminate a tag at the > in a quoted JavaScript arrow. Tokenize HTML with
Python's quote-aware parser and rename only real @ attributes to inert names.
Original evidence is never rewritten; attributes, hrefs and text retain their
values. This is parsing normalization, not a regex deleting JavaScript text.
"""
import re
from html import escape
from html.parser import HTMLParser

from bs4 import BeautifulSoup as _BeautifulSoup


def normalize_event_attributes(markup):
    if not isinstance(markup, str) or not re.search(r'\s@[\w.:-]+\s*=', markup):
        return markup
    starts = [0] + [m.end() for m in re.finditer('\n', markup)]
    replacements = []

    class EventTokenizer(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if not any(name.startswith('@') for name, _ in attrs):
                return
            raw = self.get_starttag_text()
            line, column = self.getpos()
            offset = starts[line - 1] + column
            names = {name for name, _ in attrs}
            encoded = []
            for name, value in attrs:
                if name.startswith('@'):
                    name = 'data-crawl-event-' + name.encode('utf-8').hex()
                    while name in names:
                        name += '-event'
                    names.add(name)
                encoded.append(name if value is None else name + '="' + escape(value, quote=True) + '"')
            replacement = '<' + tag + ' ' + ' '.join(encoded) + ('/>' if raw.rstrip().endswith('/>') else '>')
            replacements.append((offset, offset + len(raw), replacement))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

    parser = EventTokenizer(convert_charrefs=False)
    parser.feed(markup)
    parser.close()
    chunks, end = [], 0
    for start, stop, replacement in replacements:
        chunks.extend((markup[end:start], replacement))
        end = stop
    chunks.append(markup[end:])
    return ''.join(chunks)


def parse_soup(markup, features='lxml', **kwargs):
    return _BeautifulSoup(normalize_event_attributes(markup), features, **kwargs)
