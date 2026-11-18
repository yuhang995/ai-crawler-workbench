# Diagnosing recover_import

`recover_import` is a recovery stage, not necessarily the root failure. A message such as “父商品缺失或未完成发布: X” means the recovery worker cannot find a successfully published primary record for `X`. It does not prove the CSV `Parent` column is wrong.

## Required diagnosis

1. Locate `X` across `SKU`, `Parent`, `Product ID`, and `Variant ID`.
2. Determine the row's declared `Type` and whether its relationship is valid.
3. Read the import-worker JSON and main log. Find the earliest error for `X` before `recover_import`.
4. Record the HTTP status, API error code, request stage, and original response.
5. Check WordPress/WooCommerce for the SKU in published products, drafts, private products, and Trash.

Common earlier causes include duplicate SKU, a stale draft from a prior attempt, image/media failure, custom-field rejection, authentication/permission failure, timeout, or a partially committed product.

Never “fix” this message by changing a `simple` row to `variable` or filling `Parent` without source evidence. If the supplied logs contain only the recovery message, state that the root cause remains unconfirmed and request the diagnostic JSON or the earlier log section.
