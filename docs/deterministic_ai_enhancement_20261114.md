# 确定性增强与 AI 自检联合改造验收

本轮修改直接进入主工程 core 与 extract_categories.py。两站在线验收使用当前源码的 CLI 入口；没有把历史页面回放当作在线成功，也没有启动真实 AI 模型会话。

## 修改

- 保留合法 Shopify tag 子路径与同域集合规范跳转支持；原始 URL 与最终 URL 分开保存、绑定校验。
- 目标审核仍检查全部唯一目标，4 个工作线程，每线程 0.2 秒请求间隔。429 使用全站共享退避截止时间，其他线程在下一次请求前一起等待；任务结束恢复 Client 原设置。
- 两套目录解析统一识别带箭头/标点的通用 CTA，清洗独立角标与评分；独立 New/Sale 分类和 New Releases 等完整名称保留。已有 /products/ 黑名单仍生效。
- header 导航兜底按有效分类链接数量选择；移动、drawer、footer 和显式隐藏项不参与，平分仍判为不确定。
- AI 沙盒自动生成 check_nav.py，并拷贝 pitfalls.md。Prompt 第一行要求先读避雷手册；AI 必须先保存决策、运行预检，在同一会话修错直至 OK。
- 自检检查 load_navigation 与 validate_navigation；结果绑定候选、决策、审核范围、证明和原始 HTML。最终决策仅改变 JSON 缩进不会误杀，实质改动必须重检，主程序退出后还会独立校验。
- Python 可补 roots/links/branches、来源引用及结构性审查字段；前提是保存 DOM 能独立重建每个候选 key→URL 的归属。只有 href 存在但父级不成立时拒绝补证，保留显式证据审查通道。
- confirmed_candidate 仍锁定；不自动生成 reopened_confirmed。已有否定审查、非法来源、黑名单、伪造 URL、错误父子前缀、缺少必需动态快照均拒绝。

## 验证

全部 unittest 142/142 通过（54.260 秒）；十站历史回放 10/10，目录数量与上轮一致。新增测试包括真实子进程自检失败/修正闭环、自动补证负例、角标与 CTA、共享 429 冷却；上轮 tag/重定向/并发测试继续通过。

真实 CLI 测试均为本轮第一次任务即通过：

| 网站 | 目录项 / 唯一目标 | 总耗时 | 目标审核阶段 | AI 调用 |
| --- | ---: | ---: | ---: | ---: |
| iniushop.com | 31 / 30 | 32.963 秒 | 22.510 秒 | 0 |
| nodpod.com | 3 / 3 | 10.187 秒 | 4.409 秒 | 0 |

自检示例：错误候选返回 ERROR、退出码 1；修改候选后返回 OK、退出码 0。成功调用含 Python 启动耗时 1.5004 秒，校验本身 0.1198 秒。两秒是小型离线任务的实测结果，不是对任意大小 HTML 的硬时限；真实模型遵循 Prompt 的效果没有伪装成在线实测。

## 性能边界

120 个模拟目标、240 次内存响应、真实每线程限速和完整发布校验：13.642 秒；4 个工作线程，最小同线程请求间隔 0.203 秒。模拟没有互联网延迟，不能证明 80～120 个真实分类在 15 秒内完成。INIU 实际目标审核为 22.510 秒，故整站 15 秒性能要求尚未达成，不通过抽检或省略目标验收掩盖。

## 结果文件

- iniushop.com：[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/iniushop.com/url.json)、[本次报告](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/iniushop.com/report.json)。
- nodpod.com：[完整目录 JSON](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/nodpod.com/url.json)、[本次报告](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/nodpod.com/report.json)。
- [单元测试日志](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/unittest-final.log)。
- [自检闭环记录](D:/Users/23696/Documents/ChatGPT/新ai爬虫/runs/deterministic-ai-enhancement-20261114/selfcheck-example/selfcheck-test.json)。
