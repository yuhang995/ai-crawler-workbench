---
name: wp-product-csv-repair
description: Audit and repair WordPress/WooCommerce product CSV data, including simple/variable/variation relationships, custom metafield columns, SKU conflicts, and recover_import failures. Use for WP建站数据、WooCommerce CSV、父商品缺失、导入失败、修复WP数据 or import-worker diagnostics; do not use for Shopify-native CSV imports.
---

# WP Product CSV Repair

Repair the data contract, not merely the final error message.

## Start with the destination profile

Identify the importer before changing data:

- `legacy_custom`: columns used by `D:/pythonProject/work`, with `Type,SKU,Name,...,brand,Stock,is_upload` and optional `*(product.metafields...)` columns.
- `crawler_current`: the crawler's expanded WP output, which also carries provenance fields such as `In stock?`, `Product ID`, `Variant ID`, `Currency`, `Source SKU`, and `Variant Image`.
- `woocommerce_native`: WooCommerce's own CSV importer. Custom metafield, brand, source URL, and provenance fields require explicit column mapping or a plugin.

Preserve custom columns and their order unless the target importer contract requires a change. Never force one profile onto another.

Run the deterministic audit first:

```powershell
python scripts/audit_wp_csv.py input.csv --profile auto
python scripts/audit_wp_csv.py input.csv --recover-id 46257220026590
```

Read [references/data-contract.md](references/data-contract.md) when repairing rows or converting profiles. Read [references/recover-import.md](references/recover-import.md) when the failure mentions `recover_import`, missing parent, incomplete publish, or an import-worker log.

## Repair invariants

- `simple`: unique nonempty SKU; `Parent` must be empty. Empty attributes are allowed.
- `variable`: one parent row with unique SKU; `Parent` must be empty. Its attribute value lists must cover the child values.
- `variation`: unique SKU; `Parent` must exactly equal an existing `variable` row's SKU. Write the parent before its variations.
- A numeric SKU is still a SKU. Never infer a parent/variation relationship from its shape, Shopify ID range, name, or URL.
- Do not invent missing variants, option combinations, prices, inventory, images, categories, or parents. Rebuild relationships only from source evidence or an existing verified parent identifier.
- Do not silently discard failed rows. Report every removal or unresolved row by original CSV line, SKU, and reason.
- Preserve UTF-8 BOM, embedded newlines, HTML, Unicode characters, and RFC 4180 quoting. Write repaired CSV with `encoding="utf-8-sig"` and `newline=""` using `csv.DictWriter`.
- Do not overwrite the supplied file. Produce `<stem>.repaired.csv` plus `<stem>.repair-report.json` unless the user chooses another path.

## Price and stock rules

- Prices and stock must parse as finite, nonnegative numbers.
- In `woocommerce_native`, leave `Sale price` empty when there is no discount; a nonempty sale price should normally be lower than `Regular price`.
- In `legacy_custom`, equal sale and regular prices are an established convention. Warn but do not change them without confirming the downstream importer.
- Preserve artificial stock conventions such as `1000`/`1000.00` when they are intentional; do not present them as verified source inventory.

## Validation after repair

Re-run the audit on the repaired copy. Require zero hard errors. Then verify:

1. row and SKU counts reconcile with the report;
2. all variation parents exist, are `variable`, and precede their children;
3. no field shifted because of commas, quotes, HTML, or embedded newlines;
4. image and source URLs remain absolute HTTP(S) URLs;
5. the target importer can map every nonstandard column.

For a server-side failure, a clean CSV does not prove import success. Report the earliest API/publication error separately from the later recovery error.
