# 当前规则更正（2026-11-14）

集合底账不再自动补进目录；实际菜单归属须独立验证。旧版平铺底账的验收结论不能证明目录正确，以下相关实用模式记录仅为历史。The Perfect Jean 已恢复 menu-dropdown 的列分组，工作台拒绝旧版仅链接核验的 practical 产物。

# 导航摘要与视觉审核

当前工作台默认采用 `shopify_practical`：底账补充平级集合、结构疑点写 `warnings.json`、最多四个 HTTP 代表目标抽检；默认不调用目录或视觉 AI。下文严格模式的截图/证明门槛保留用于显式 AI 模式和历史回放。实用模式视觉审核失败仍保留真实 `passed:false`，作为警告继续源 URL 与商品校验，绝不把未完成的视觉检查写成通过。

CLI 使用 `python extract_categories.py --site https://nodpod.com/ --out runs/new-run --visual-check`；主工程 `app.py` 同样接受 `--visual-check`。网页新任务勾选“视觉双重确认”。已运行的旧服务需重新加载代码后才显示新选项。

默认关闭时，确定性目录路径不会调用视觉 AI。进入目录 AI 前，由 Python 一次性展开桌面顶层菜单，生成 nav_digest.json 和独立来源台账，附加首屏及最多五张菜单图。模型仅返回层级标签与 URL 索引；工具被禁用，临时目录不提供原始 HTML。Python 对照原始 href 生成候选、证明与决策并运行 check_nav.py。开启视觉开关时，发布前另外执行一次视觉会话，模型使用 medium，排队与会话共享最多 90 秒预算；截图及 HTTP 验证不计入这 90 秒。

桌面视口为 1440×1000，locale 为 en-US，写入 US/USD 偏好 Cookie（不是市场实际生效保证）。证据保存到 `evidence/home-visual-desktop.*`、`home-visual-full.*` 和分段 `home-visual-section-*`；菜单截图使用 `home-menu-*`。每张截图配对 HTML、DOM 节点、坐标和页面状态。首页最多分 16 段，长图超过 8000px 改用分段。截图缺失或不一致会令视觉检查 inconclusive；实用模式记警告，严格模式停止发布。

AI 只核对桌面一级根及首页分类区块，不核对所有未展开子菜单。截图决定可见性与分组，URL 只能来自真实 DOM。首屏缺席不直接代表移动端泄漏，合法桌面抽屉、切换菜单要保留；首页单品推荐不作为分类。精简 DOM 直接随提示提供，避免视觉会话依赖读取文件的命令权限。

`visual_audit.json` 状态为 `passed`、`needs_repair` 或 `inconclusive`。移动副本发现项必须引用隐藏 mobile/drawer 容器的真实 DOM 节点、覆盖该分支 URL，且无可见桌面控件关联。仅有这类疑点时自动剪枝，记录 auto_pruned_roots，保留原始候选与证明，重新验证非空剩余目录。实用模式其余视觉疑点记警告；严格模式仍阻断。发布版本绑定候选及来源；实用模式 `navigation_targets.json.check` 明确区分 HTTP 抽检与仅来源核验，不声称逐分类商品范围已验证。

初版验收记录见 `visual_audit_acceptance_20261114.json`：Nodpod 第三轮 3 条目录通过；INIU 曾因字体等待超时被阻断。

字体修复后，只有明确的字体等待超时才回退 Chromium CDP 捕获当前实际像素，PNG 标记 capture.backend；不删除字体、不修改页面文案或 URL。截图稳定性仅比较 header/顶部导航，最多重拍三次；main 轮播变化不再触发导航布局失败。首页图片和 DOM 仍保存配对证据。公开 JSON 恢复流程允许已出现同域导航的首页加载超时，但最终仍必须解析真实 JSON 并核对来源域名和路径。

修复后的 INIU 平台任务 `7d3db9b2e4e8e0e4` 完整通过：31 条目录、33 条商品列表链接、3 个商品样本、21 个变体；整单 196.966 秒，视觉阶段 81.546 秒。平台下载的 ZIP 已独立解压校验通过。此处“完整”指目录、默认分类列表、前三商品样本、校验和打包，并非全站商品下载。

五站首轮平台测试固定取自 `D:/pythonProject/work`，全部打开视觉审核，不根据结果换站。详见 `fontfix_platform_acceptance_20261114.json`。模拟 Menu 测试只证明阻断流程；本次 Touchland 在线审核还实际发现候选 JSON 混入隐藏的 Menu Close 移动分组。截图只代表当次页面状态，视觉模型不能保证零误判。

目录 AI 总预算恢复为 180 秒，最多两轮结构输出；NAVIGATION_TIMEOUT_SECONDS 或显式 timeout 只能缩短该上限。navigation_invocation.json 和 navigation_timing.json 记录实际预算、排队、调用次数和摘要字节数。5 KB 是摘要目标，不靠截断真实分类来满足大小限制。600 秒历史复测保留用于对比，不改写首次成功率。
