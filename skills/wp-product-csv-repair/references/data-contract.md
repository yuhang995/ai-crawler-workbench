# WP CSV data contracts

## Shared required columns

`Type`, `SKU`, `Name`, `Description`, `Sale price`, `Regular price`, `Categories`, `Tags`, `Images`, `Parent`, `brand`, `Stock`, and `is_upload`.

Attribute columns occur in name/value pairs: `Attribute N name` and `Attribute N value(s)`. A missing mate is a schema error. Blank pairs are allowed for simple products.

## Legacy custom profile

This profile commonly adds content columns named like `Ingredients (product.metafields.c_f.zdy_tabs1)` and may use `External URL` or `url`. These are custom import mappings, not standard WooCommerce behavior. Keep equal sale/regular prices unchanged unless the actual importer contract is being modernized.

## Current crawler profile

The crawler may add `In stock?`, `External URL`, `Product ID`, `Variant ID`, `Currency`, `Source SKU`, and `Variant Image`. These fields preserve provenance and variant-image binding. Dropping them can weaken replay validation even if WordPress ignores them.

## Native WooCommerce profile

WooCommerce recognizes common product columns but may not automatically map project-specific brand, source URL, metafield, or provenance columns. Confirm the import mapping. For native semantics, an ordinary-priced product should have a blank sale price.

## Relationship example

```csv
Type,SKU,Name,Parent,Attribute 1 name,Attribute 1 value(s)
variable,shirt,Shirt,,Color,"Red, Blue"
variation,shirt-red,Shirt,shirt,Color,Red
variation,shirt-blue,Shirt,shirt,Color,Blue
```

Do not fabricate a variable parent around simple products merely because their SKUs are numeric or originated as Shopify variant IDs.
