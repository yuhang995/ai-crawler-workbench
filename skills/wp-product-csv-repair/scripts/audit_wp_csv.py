#!/usr/bin/env python3
"""Read-only structural audit for project/WooCommerce product CSV files."""
import argparse
import csv
import io
import json
import math
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


BASE_REQUIRED = {
    'Type', 'SKU', 'Name', 'Description', 'Sale price', 'Regular price',
    'Categories', 'Tags', 'Images', 'Parent', 'brand', 'Stock', 'is_upload',
}
ALLOWED_TYPES = {'simple', 'variable', 'variation', 'external', 'grouped'}


def issue(level, code, message, line=None, sku=None):
    value = {'level': level, 'code': code, 'message': message}
    if line is not None:
        value['line'] = line
    if sku:
        value['sku'] = sku
    return value


def finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def http_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)
    except ValueError:
        return False


def detect_profile(fields):
    if {'Product ID', 'Variant ID', 'Currency', 'Source SKU', 'Variant Image'} & set(fields):
        return 'crawler_current'
    if any('product.metafields.' in field for field in fields) or 'is_upload' in fields:
        return 'legacy_custom'
    return 'woocommerce_native'


def audit(path, profile='auto', recover_id=None):
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    fields = reader.fieldnames or []
    rows = list(reader)
    selected = detect_profile(fields) if profile == 'auto' else profile
    issues = []
    missing = sorted(BASE_REQUIRED - set(fields))
    if missing:
        issues.append(issue('error', 'missing_columns', 'Missing columns: ' + ', '.join(missing)))
    if any(None in row for row in rows):
        issues.append(issue('error', 'shifted_row', 'At least one row has more fields than the header'))

    sku_rows = {}
    parent_positions = {}
    recover_matches = []
    attr_numbers = sorted({int(match.group(1)) for field in fields
                           for match in [re.fullmatch(r'Attribute (\d+) name', field)] if match})
    for number in attr_numbers:
        mate = f'Attribute {number} value(s)'
        if mate not in fields:
            issues.append(issue('error', 'attribute_pair_missing', f'Missing {mate}'))

    for line, row in enumerate(rows, 2):
        typ = (row.get('Type') or '').strip().casefold()
        sku = (row.get('SKU') or '').strip()
        parent = (row.get('Parent') or '').strip()
        if typ not in ALLOWED_TYPES:
            issues.append(issue('error', 'invalid_type', f'Unsupported Type: {typ!r}', line, sku))
        if not sku:
            issues.append(issue('error', 'missing_sku', 'SKU is empty', line))
        elif sku in sku_rows:
            issues.append(issue('error', 'duplicate_sku', f'Duplicate SKU; first seen on line {sku_rows[sku]}', line, sku))
        else:
            sku_rows[sku] = line
        if typ == 'variable':
            parent_positions[sku] = line
            if parent:
                issues.append(issue('error', 'variable_has_parent', 'Variable parent must not have Parent', line, sku))
        elif typ == 'simple' and parent:
            issues.append(issue('error', 'simple_has_parent', 'Simple product must not have Parent', line, sku))
        elif typ == 'variation' and not parent:
            issues.append(issue('error', 'variation_missing_parent', 'Variation requires Parent SKU', line, sku))

        for column in ('Sale price', 'Regular price', 'Stock'):
            value = (row.get(column) or '').strip()
            if value and (not finite_number(value) or float(value) < 0):
                issues.append(issue('error', 'invalid_number', f'{column} is not a finite nonnegative number: {value!r}', line, sku))
        sale, regular = (row.get('Sale price') or '').strip(), (row.get('Regular price') or '').strip()
        if sale and regular and finite_number(sale) and finite_number(regular):
            if float(sale) > float(regular):
                issues.append(issue('error', 'sale_above_regular', 'Sale price exceeds Regular price', line, sku))
            elif float(sale) == float(regular):
                level = 'warning' if selected == 'woocommerce_native' else 'info'
                issues.append(issue(level, 'sale_equals_regular', 'Sale price equals Regular price', line, sku))

        for column in ('External URL', 'url', 'Product URL'):
            value = (row.get(column) or '').strip()
            if value and not http_url(value):
                issues.append(issue('error', 'invalid_url', f'{column} is not an absolute HTTP(S) URL', line, sku))
        for value in ((row.get('Images') or '').split(',')):
            value = value.strip()
            if value and not http_url(value):
                issues.append(issue('error', 'invalid_image_url', f'Invalid image URL: {value!r}', line, sku))

        if recover_id and any(recover_id in (row.get(column) or '')
                              for column in ('SKU', 'Parent', 'Product ID', 'Variant ID')):
            recover_matches.append({'line': line, 'type': typ, 'sku': sku, 'parent': parent,
                                    'name': row.get('Name', ''),
                                    'matched_fields': [column for column in ('SKU', 'Parent', 'Product ID', 'Variant ID')
                                                       if recover_id in (row.get(column) or '')]})

    variable_skus = set(parent_positions)
    child_counts = Counter()
    for line, row in enumerate(rows, 2):
        if (row.get('Type') or '').strip().casefold() != 'variation':
            continue
        sku = (row.get('SKU') or '').strip()
        parent = (row.get('Parent') or '').strip()
        if parent:
            child_counts[parent] += 1
            if parent not in variable_skus:
                issues.append(issue('error', 'orphan_variation', f'Parent {parent!r} is not an existing variable SKU', line, sku))
            elif parent_positions[parent] >= line:
                issues.append(issue('error', 'parent_after_child', f'Parent {parent!r} must precede its variation', line, sku))
    for parent, line in parent_positions.items():
        if not child_counts[parent]:
            issues.append(issue('warning', 'variable_without_children', 'Variable parent has no variation rows', line, parent))

    counts = Counter((row.get('Type') or '').strip().casefold() for row in rows)
    return {
        'file': str(path.resolve()), 'encoding': 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8',
        'profile': selected, 'rows': len(rows), 'columns': len(fields), 'types': dict(counts),
        'errors': sum(value['level'] == 'error' for value in issues),
        'warnings': sum(value['level'] == 'warning' for value in issues),
        'info': sum(value['level'] == 'info' for value in issues),
        'issues': issues, 'recover_matches': recover_matches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_file')
    parser.add_argument('--profile', choices=('auto', 'legacy_custom', 'crawler_current', 'woocommerce_native'), default='auto')
    parser.add_argument('--recover-id')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        report = audit(args.csv_file, args.profile, args.recover_id)
    except (OSError, UnicodeError, csv.Error) as error:
        report = {'file': str(Path(args.csv_file).resolve()), 'errors': 1, 'warnings': 0,
                  'issues': [issue('error', 'csv_read_failed', str(error))]}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.write_text(output + '\n', encoding='utf-8')
    raise SystemExit(1 if report.get('errors') else 0)


if __name__ == '__main__':
    main()
