# 本地资产复用

没有复制旧项目的运行框架、宿主哈希、唯一选择器证明或多轮 CLI。

| 来源 | 新项目复用内容 |
| --- | --- |
| 用户提供 `Power-Spider-master.zip`，`modules/collection/shopify/extract_data_from_html.py` | DOMNode、菜单层级、标题回退、层级 key、首页区块规则直接提取进 nav_extractor；替换 HTMLParser 为 lxml，修正 void 元素与根 nav，收紧黑名单，保留语义查询和锚点 |
| 同 ZIP 的 `shopify_detector.py` | SHOPIFY_MARKERS、THIRD_PARTY_MARKERS、static_graphql_candidates、static_storefront_config 直接提取；替换 scrapling 为现有 curl_cffi 并行 HTTP |
| `D:/pythonProject/work/测试/shopify/get_detail_url.py` | collection products.json 分页与 handle URL 生成；新增前端交集保序，不能用 feed 顺序替代页面顺序 |
| 同目录 `3.ShopifyCrawl_sort.py` | 真实 variants 遍历、variant_ids 专属图片映射、用户 CSV 字段；移除旧 Cookie/域名，修正 Decimal、促销价格、未知库存 |
| 旧项目 `framework/navigation_evidence.py`、`catalog_browser.py` | 通用 CTA 路径兜底、菜单按钮展开/hover 后重读 DOM 的思路；移除 selector proof 与 navigation_chain |
| 旧项目 `market_policy.py` | 当前国家与当前币种的显式信号核对，拒绝冲突，不把 en-US 语言当成美国市场 |

Power-Spider 原 ZIP 路径：`C:/Users/23696/xwechat_files/wxid_3m599f9zki4412_92f2/msg/file/2026-11/Power-Spider-master.zip`。

数据口径核对：[Shopify Ajax Product API](https://shopify.dev/docs/api/ajax/reference/product)；CLI 参数按本机 `codex exec --help` 核对，workspace-write 出站网络参数见 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。
