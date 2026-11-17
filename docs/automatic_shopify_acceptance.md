# 全自动 Shopify 框架本次验收

本次已完成框架改造和自检。核心目标是让目录成为有证据、有版本的发布产物；无法证实正确时自动阻断下游，保留错误和断点。不能承诺任何网站、任何时间都能采集成功。

## 实测结果

| 网站 | 本次通过范围 | 目录项 | 商品 | 变体 | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| jonesroadbeauty.com | 全目录、全商品 | 7 | 91 | 422 | 通过 |
| k18hair.com | 全目录、全商品 | 4 | 28 | 28 | 通过 |
| explodingkittens.com | 全部目录目标、一个完整分类、前三个商品 | 18 | 3 | 3 | 通过 |
| lolablankets.com | 全部目录目标、一个完整分类、前三个商品 | 22 | 3 | 12 | 通过 |
| maryruthorganics.com | 仅目录及全部目标 | 35 | 未采集 | 未采集 | 通过 |

Jones Road 与 K18 是本次全量案例。Lola 与 Exploding Kittens 是基础验收，不能当作整站商品全量完成。MaryRuth 只验证了目录与目标，不宣称已完成商品采集。

所有上述商品交付包都已解压，在独立目录运行 verify.py 复验通过。K18 首次全量任务因带 ™ 的商品 URL 编码比较错误而失败；修复后复用 13 个已完成商品断点、继续采集剩余 15 个，最终 28 个商品通过。首次 failure.json、原始失败汇总及 resume_result.json 均保留，最终状态以 resume_result.json 和 final_runtime_verification.json 为准。

Lola 的 22 个目录项对应 20 个唯一目标。两个特殊落地页的根集合 feed 为空，但存在有限、明确的真实商品卡片；逐卡核验商品接口后通过，来源单独记录在 card_sources，未伪造集合 feed。随后使用本次保存的真实证据离线重放完整落地页采集与变体验证：Holiday 37 个商品/127 个变体，Westward 27 个商品/92 个变体；这是保存证据回放，不另计一次在线全量成功。

## 没有通过的在线范围

- Knix 最近一次在线任务在目录目标阶段失败：48/55 个目标的静态商品列表无法确认。已增加浏览器渲染分支，但本轮后续未重新完整在线验收，不能报告已恢复。
- Leatherman 最近一次在线任务有 16/46 个目标失败，首个原因是 HTTP 429。已加入单站顺序校验、共享限速与有界恢复，但没有用离线通过代替线上恢复结论。
- D:/pythonProject/work 发现的 172 个域名只是候选清单，没有全部在线跑完。

因此，本报告的 all_tested_websites_passed 为 false。失败目标不会发布合格目录，也不会继续污染商品归属。

## 问题原因与已落地修复

1. 扁平链接提取无法证明父子归属：保留主控件、面板、原始节点及源文档哈希，独立重建并核对菜单树；共享抽屉增加桌面实际展开检查。
2. 标题混入徽章和说明、索引与奖励页混入分类：区分独立标题与描述，排除非商品集合入口；同 URL 在不同真实分组下仍保留。
3. AI 修改锁定分类缺少证据：仅补查疑点；修改已确认项必须声明重新打开原因和来源，并通过独立重建。
4. 生成 JSON 后直接进入下游：新增候选→逐目标审核→版本发布流程，绑定原始证据、目录与列表；文件变更或缺少审核立即拒绝采集。
5. 落地页不等于普通集合：分别保存子集合完整分页来源与有限显式商品卡片来源，禁止把任意商品链接当分类成员。
6. 网络波动、编码差异和单商品中断：共享限速、有界重试、一次匿名浏览器恢复、百分号解码后的身份核对、按版本绑定的商品断点。
7. 三商品样本被误当整站完成：网页/API/CLI 增加明确全量模式，校验实际全部商品；报告始终注明范围。

## 回归与自检

- 自动测试：129 项通过，包含归属、来源篡改、目标失败、范围错配、未批准目录绕过、落地页身份、真实变体和 URL 编码等。
- 十站历史页面回放全部通过：K18、Knix、Jones Road、Kosas、Kyte Baby、Laura Mercier、Leatherman、Lola、Magnatiles、MaryRuth；这仅代表保存页面的目录回归。
- Jones Road 的 91 个商品断点在禁止网络访问的测试下完整复用并通过校验。
- 网页模式切换、提交参数和 JavaScript 错误检查通过；新服务实测拒绝 full+manual 冲突参数（HTTP 400）。
- 运行代码、顶层测试脚本编译和 web/app.js 语法检查通过。tests/live 内历史浏览器执行片段含顶层 await/return，不是独立 Python 程序，不纳入运行代码编译结论；未修改这些原始记录。

## 使用入口与文件

新版平台：http://127.0.0.1:8878 。选中“全自动采集全部商品”后无需人工确认。原 8773 服务保留；自动审批拒绝停止其进程，返回原因仅为策略阻止，因此改用独立端口。

功能框架：automatic_shopify_framework.md。机器可读验收：automatic_shopify_acceptance.json。每站目录、CSV、独立包与原始证据均在以下目录：

- jonesroadbeauty.com：[runs/shopify-acceptance-20261114-041047/jonesroadbeauty.com](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041047/jonesroadbeauty.com)；[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041047/jonesroadbeauty.com/url.json)；[独立包](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041047/jonesroadbeauty.com/crawler.zip)。
- k18hair.com：[runs/shopify-acceptance-20261114-041710/k18hair.com](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041710/k18hair.com)；[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041710/k18hair.com/url.json)；[独立包](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041710/k18hair.com/crawler.zip)。
- explodingkittens.com：[runs/shopify-acceptance-20261114-041405/explodingkittens.com](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041405/explodingkittens.com)；[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041405/explodingkittens.com/url.json)；[独立包](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-041405/explodingkittens.com/crawler.zip)。
- lolablankets.com：[runs/shopify-acceptance-20261114-042620/lolablankets.com](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-042620/lolablankets.com)；[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-042620/lolablankets.com/url.json)；[独立包](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/shopify-acceptance-20261114-042620/lolablankets.com/crawler.zip)。
- MaryRuth：[目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/maryruth-framework-live/url.json)。
