---
name: web-browsing
description: 联网查资料与浏览器操作指南。当任务需要搜索网络信息、访问网页、读取在线文档、对页面截图验证、或给用户打开某个网站时，按本技能选择正确的工具组合。
keywords:
  - 搜索
  - 搜一下
  - 查一下
  - 上网
  - 网页
  - 网站
  - 浏览器
  - 打开网站
  - 打开网页
  - 截图
  - 抓取
  - 文档
  - search
  - browse
  - screenshot
---

# Web Browsing（联网与浏览器）

## 工具选择决策树

1. **用户想查资料 / 找信息 / 核对事实**
   → 先 `web_search` 拿到候选链接，再用 `fetch_url` 读最有希望的 1~2 个详情。

2. **用户给了具体网址，要读内容**
   - 静态页（文档、博客、API 参考）→ `fetch_url`（快，直接拿正文）
   - 抓不到正文或页面明显是 JS 应用（SPA、控制台类）→ `browser_snapshot`

3. **需要"看"页面而不是读文字**（界面验证、布局确认、给用户展示效果）
   → `browser_screenshot` 截图到 `.browser/` 目录，把返回的文件路径告诉用户。

4. **用户说"帮我打开 XX 网站"**
   → `open_browser`，会在用户本机弹出可见的浏览器窗口。注意：这是给用户看的，
   你自己拿不到内容；若你还需读取页面内容，配合 `fetch_url` 或 `browser_snapshot`。

## 注意事项

- 所有浏览器工具均为只读（SAFE），但 `open_browser` 会打扰用户——只在明确需要展示时使用。
- `browser_screenshot` / `browser_snapshot` 依赖本机的 `agent-browser` CLI（无头 Chromium），
  报"未找到"时提示用户执行：`npm install -g agent-browser && agent-browser install`。
- 搜索结果仅供参考，重要数字（价格、版本号、日期）尽量打开原文链接核对。
- 抓取内容可能很长，注意 max_chars 参数，必要时分段抓取。
