# 运行时模块边界

## 当前主链路

- `app.py`：本地工作台、任务队列、取消、历史清理和更新入口。
- `core/pipeline.py`：站点探测、目录、列表、商品、验证与打包主编排。
- `core/simple_nav.py`：默认目录提取与自愈入口。
- `core/shopify_fastpath.py`：Shopify/Hydrogen 列表、商品与导出。
- `core/custom_site.py`：非 Shopify 适配器路由。
- `core/common.py`：HTTP、代理池、访问恢复、取消检查与通用 IO。
- `core/validator.py`：产物与原始证据独立验真。
- `core/ai_repair.py`：受控的单样本商品修复。

## 遗留兼容模块（暂不删除）

- `core/navigation_browser_tool.py`：旧动态菜单工具；仍被 `tests/test_navigation_browser.py`
  直接导入，并被历史动态导航提示契约引用，因此不是可安全删除的死代码。
- `core/navigation_rules.py`、`core/navigation_guard.py`、`core/navigation_release.py`、
  `core/practical_navigation.py`：默认 `simple_nav` 之外的历史证据/回放/手工审核兼容层。
- `core/navigation_ai.py`、`core/hybrid_navigation.py`：旧目录 AI 证明链与历史回放仍有测试依赖。

删除遗留模块前，必须先迁移对应回归测试和独立产物契约，不以“默认主链路不调用”作为直接删除依据。
