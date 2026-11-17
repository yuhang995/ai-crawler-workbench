---
name: category-json
description: Build ecommerce category url.json files from visible navigation and homepage category blocks.
---

# Category JSON

- Read the supplied compact navigation evidence before changing files.
- Output one JSON object mapping cumulative category paths to absolute URLs.
- Use `一级`, `一级,一级 二级`, and `一级,一级 二级,一级 二级 三级` key structure.
- Preserve visible spelling, capitalization, punctuation, and branch ownership.
- Keep at most three levels. Fold deeper leaves into level three.
- Keep the same URL under different real menu branches when both are visible.
- Use only URLs present in supplied HTML evidence. Never invent a URL.
- Exclude product cards, account, cart, search, support, articles, policies, and gift cards.
- Treat drawer navigation as primary only when it is the site's actual main menu.
- Resolve `View All`, `Shop Now`, and similar CTA labels from their owning heading.
- Remove badges such as New, Sale, ratings, and Free Engraving from labels.
- Preserve valid category filters and query parameters.
- Add unique homepage category or collection blocks after the menu hierarchy.
- Do not import a sitemap or collection feed as navigation.
- Run the provided `check_nav.py` before finishing and continue until it prints `OK`.
