# 2026-11-13 实际验收

环境：Windows，现有 Anaconda exe310 Python 3.10.20；未创建 venv；两站标准路径均为 0 次 LLM 调用。

| 站点 | 导航数 | 本次完整分类 | 前端/feed 交集链接 | 样本商品 | 真实变体 | 自动运行含 ZIP 总耗时 |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| gcioutdoor.com | 26 | Rockers | 35 | 3 | 15（8+2+5） | 5.716 秒 |
| blackstoneproducts.com | 26 | Griddles | 18 | 3 | 3（1+1+1） | 4.256 秒 |

两站都是本次在线抓取，既有本地缓存未作为网络请求结果输入。验收程序同时运行两站，计时包含首页、探测、分类列表、详情、校验和 ZIP，低于 1–3 分钟目标。外部 CDN 缓存及网络波动会影响复测。

静态导航分别为 0.1298 / 0.1159 秒；含网络的前三商品定位为 1.1534 / 0.3754 秒，CPU 解析为 0.3094 / 0.0604 秒。**0.1 秒导航和 0.5 秒定位不是全部达到的硬时限**，保留真实数据，不将网络时间排除后冒充总耗时。结果校验分别为 0.4419 / 0.2147 秒。

验证记录：

- 22 项离线单元/流程测试通过，覆盖黑名单、home 层级、View All、动态补抓条件、分类锚点、前端交集顺序、变体卡去重、分页、金额单位、真实变体/图片、产物伪造拒绝、四类站点路由。
- 手动模式在 3 样本校验后确实阻塞，确认前没有 ZIP，确认后生成 ZIP。
- Playwright 在实际网页提交 Blackstone 手动任务、点击确认、下载 ZIP 成功；无 JavaScript 错误。截图为 `workbench-preview.png` / `workbench.png`。
- 两站 ZIP 均解压到新的临时目录，用单独 Python 进程运行 `verify.py` 通过；没有在线重采，也没有依赖旧项目路径。
- 单次 CLI 调用通过 mock 失败/重入测试；没有为了测试消耗真实 CLI 会话，未宣称验证了真实非标站适配成功率。
- GCI 额外跑通全部 26 个分类链接，商品详情仍仅 3 个样本。该扩展运行约 16.4 秒，26 类整体结果校验 2.7 秒，不能将全目录校验宣称为 1 秒内。

Blackstone 全目录检查发现 3 个失效锚点：

- `/collections/outdoor-kitchens#built-in-griddles`
- `/collections/pizza#conversion-kits`
- `/collections/pizza#accessories`

这些入口保留在真实导航中，但选择它们会明确报错。没有删除锚点扩大采集范围，也没有把全目录检查宣称成功。Griddles 分类按要求执行 HTML/feed 交集，页面不属于 feed 的展示商品记录于 excluded_not_in_collection。

原始自动验收数据见 `e2e_report.json`。交付包位于 `runs/acceptance/<域名>/crawler.zip`。
