"""Copy the v1 docs pages (atomicdb/templates/atomicdb/docs.html and docs/*.html)
into this directory as plain HTML with placeholders, for pages/docs.rs.

    python extract.py <path to atomicdb/templates/atomicdb>

Each file keeps its `{% block title %}` on the first line (`<!-- title: ... -->`)
and the body of `{% block doc %}`.  The few template values become
placeholders: __SUBTREE_CAP__, __MULTI_REQUEST_CAP__, __CHILDREN_CAP__,
__QUEUE_CAP__, __V__ (the static version query).  Re-run it when v1's docs
change; the files are checked in.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUBS = [
    (r"\{% static '([^']+)' %\}", r'/static/\1'),
    (r'\{% if static_version %\}\?v=\{\{ static_version \}\}\{% endif %\}', '__V__'),
    (r'\{\{ subtree_cap \}\}', '__SUBTREE_CAP__'),
    (r'\{\{ multi_request_cap \}\}', '__MULTI_REQUEST_CAP__'),
    (r'\{\{ children_cap \}\}', '__CHILDREN_CAP__'),
    (r'\{\{ queue_cap \}\}', '__QUEUE_CAP__'),
    (r'\{% load static %\}\n?', ''),
    (r'\{% comment %\}.*?\{% endcomment %\}', ''),
]


def extract(src, dst):
    t = open(src, encoding='utf-8').read()
    title = re.search(r'\{% block title %\}(.*?)\{% endblock %\}', t, re.S).group(1).strip()
    body = re.search(r'\{% block doc %\}(.*?)\{% endblock %\}', t, re.S).group(1)
    for pat, rep in SUBS:
        body = re.sub(pat, rep, body, flags=re.S)
    left = re.findall(r'\{[{%].*?[%}]\}', body)
    if left:
        raise SystemExit(f'{src}: template tags left: {left}')
    with open(dst, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'<!-- title: {title} -->\n{body.strip()}\n')


def main():
    base = sys.argv[1]
    extract(os.path.join(base, 'docs.html'), os.path.join(HERE, 'overview.html'))
    for name in os.listdir(os.path.join(base, 'docs')):
        if name.endswith('.html') and not name.startswith('_'):
            extract(os.path.join(base, 'docs', name), os.path.join(HERE, name))


if __name__ == '__main__':
    main()
