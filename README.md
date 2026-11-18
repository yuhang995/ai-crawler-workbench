# ai爬虫 3

当前正式版本：**1.0.1**。网页工作台支持每次粘贴最多 20 个站点，后台最多 4 个站点并发运行；任务卡片显示阶段进度，总览显示所有任务的总体进度。Git 克隆安装可在工作台点击“检查更新”执行安全快进更新，本地有未提交代码时会拒绝覆盖。

默认目录入口现在为 `core/simple_nav.py`：首页 HTML 清洗 → 一次无工具 Codex 整理目录 → 真实链接过滤及键名规范化。随后执行 US/USD 检查、商品列表及前三个真实商品采集、结果校验和 ZIP 打包。当前本机服务：<http://127.0.0.1:8880/>。

旧 DOM 重建、集合底账补入、视觉审计与目录哈希锁不参与默认流程；旧模块留作显式兼容和历史回放。旧验收文档描述历史版本，不代表当前入口。

## 启动

要求 Python 3.10+。本机可直接使用现有环境，无需创建 venv：

```powershell
& 'D:/ProgramData/Anaconda3/envs/exe310/python.exe' app.py
```

服务默认监听 `0.0.0.0:8773`，启动时会打印 `http://127.0.0.1:8773` 和探测到的局域网地址；同一私网内可直接共享使用。新环境先 `python -m pip install -r requirements.txt`；浏览器菜单核对与动态商品网格需要 `python -m playwright install chromium`。运行时不自动安装依赖。

默认采集一个分类和前三个商品；手动模式在采集前编辑目录。`--full` 才运行全部目录与全部商品。新目录发布报告仅说明链接出现在首页证据、通过黑名单过滤且键名已规范，不宣称 AI 层级有独立视觉认证。

## 命令行与测试

```powershell
python app.py --site https://gcioutdoor.com --out runs/gci
python app.py --site https://blackstoneproducts.com --out runs/blackstone
python app.py --site https://gcioutdoor.com --all-categories --out runs/gci-all
python -m unittest discover -s tests -v
python tests/live_e2e.py
```

本机系统 `python` 是旧版本时，请将以上 `python` 替换为启动命令中的 Python 3.10 绝对路径。在线测试脚本不会启动全量详情。

## 目录

```text
app.py                  标准库 HTTP 工作台 / CLI
web/                    静态界面，无构建工具
core/
  detector.py           Shopify / GraphQL / 搜索应用 / 自建站与市场探测
  simple_nav.py         默认：HTML 清洗、单次 AI、来源链接过滤
  nav_extractor.py      历史静态解析器（显式旧模式）
  navigation_guard.py   历史目录核验（默认旁路）
  shopify_fastpath.py    列表交集保序、分页、真实变体、CSV
  ai_worker.py          非标任务唯一一次 codex exec
  validator.py          原始响应与结果级校验
  pipeline.py           自动/手动流程与独立运行包
  common.py             有限重试、线程隔离 HTTP、原子 JSON 写入
tests/                  离线测试和显式在线验收
docs/                   数据契约、复用来源、验收报告、界面截图
runs/                   每任务独立产物（不进入 Git）
```

## 产物与规则

- `url.json` 保留全部真实导航；`detail_url.json` 只包含本次实际采完的分类，范围写在 `sample_scope.json`。
- 商品顺序是前端商品区锚点与分类 feed 的交集，按商品 ID 去重；推荐、最近浏览、隐藏模板不算列表。非分类成员记录在 `listings.json` 的 `excluded_not_in_collection` 中。
- 不移除有语义的分类查询参数/锚点；不存在的锚点报错，不扩大分类。动态加载无法确定终点时交单次非标适配。
- `result.csv` 每行一个真实变体；`result_clean.csv` 按商品/变体 ID 稳定去重；`shopify_import.csv` 为 draft 导入文件。未知库存留空。
- `.json`/分类 feed 的金额按美元，`.js` 按整数美分，使用 Decimal；促销价仅在低于原价时写入。变体图片按 variant_ids / featured_image 关联。
- ZIP 包含配置、采集脚本、公共运行时、源响应、三样本和 CSV，无需二次在线验证或建 venv。解压后 `python verify.py` 可离线复验。
- 默认目录会话只启动一次 CLI。失败保留日志；不要求 navigation_chain 或宿主哈希。

## 默认目录流程与边界

- Python 保留 header/nav 原始父子结构（包括 drawer）及关联 aria-controls 面板，去脚本、样式、图片、冗长属性，附上 main 分类卡片与真实同域分类 URL。
- 静态 header/nav 没有分类链接才进行一次 10 秒预算的浏览器展开。浏览器启动、关闭和网络的实际时间会有开销。
- `codex exec` 一次调用：read-only、Medium、stdin 输入、JSON schema 输出、90 秒超时。不加载用户工具配置，禁用 shell/browser/MCP/apps/plugins/multi-agent，不传图片、不启动修复轮次。
- 只过滤虚构/黑名单链接、修正累积父级键名；不重建 DOM、不逐分类请求、不补入 collections feed。过滤产生 warnings；模型超时、空目录和商品访问失败不会伪装成成功。
- `evidence/slim_nav.txt` 保存完整实际输入。10–25 KB 是目标而非裁剪上限；完整大型菜单可能超限，记录 `slim_over_25kb`，不会静默丢掉子菜单。
- 目录输出仍可能有 AI 理解偏差，可在手动确认中编辑。速度由 HTML 大小、模型服务与网络决定，不保证 15–25 秒。
- 商品身份、价格、真实变体、市场及商品断点绑定继续验证；取消的是旧目录锁，不是商品数据真实性检查。
- 公共 JSON 的一次 403 浏览器恢复直接打开分类页（commit），等商品链接或 3 秒，再同源 fetch 并保存 Cookie。真实访问限制仍可能失败。

仅提取目录：`python extract_categories.py --site https://example.com --out runs/example-new`。显式旧模式可用 `--navigation-mode practical` / `ai`，用于回归；网页默认 simple。

本机需要已有 Codex 登录与 Playwright Chromium。验收记录会区分目录成功、商品成功和全站全量；默认三个样本成功不等于全站所有分类均已采完。
