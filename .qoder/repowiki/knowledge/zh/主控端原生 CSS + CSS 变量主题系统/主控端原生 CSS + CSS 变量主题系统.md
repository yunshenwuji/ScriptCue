---
kind: frontend_style
name: 主控端原生 CSS + CSS 变量主题系统
category: frontend_style
scope:
    - '**'
source_files:
    - controller/index.html
    - controller/style.css
    - controller/app.js
---

## 1. 采用的样式方案

- **纯原生 CSS**：主控端（`controller/`）仅包含 `index.html`、`style.css`、`app.js`，未引入任何 CSS 框架（如 Bootstrap、Tailwind）、预处理器（Sass/Less）或构建工具。
- **CSS 自定义属性（:root 变量）**作为设计令牌中心，集中定义全局色板与尺寸：
  - 背景/卡片/文字：`--bg`、`--card`、`--ink`、`--muted`
  - 语义色：`--primary` / `--primary-dark`、`--ok`、`--warn`、`--bad`
  - 装饰：`--line`、`--radius: 14px`
- 通过 `<meta name="theme-color" content="#16213e">` 设置移动端浏览器地址栏主题色，与深色 header 保持一致。

## 2. 关键文件

- `controller/index.html` — 页面结构，使用 BEM 风格类名（`brand`、`pill`、`card`、`room-head`、`agent`、`controls`、`receipts` 等），按“首页 → 房间控制台”两个 `<section>` 切换视图。
- `controller/style.css` — 全部样式实现，约 216 行，覆盖布局、组件、状态、响应式。
- `controller/app.js` — 通过 DOM 操作为元素添加/移除 `.online`、`.offline`、`.bad-clock`、`.holding` 等状态类，由 CSS 驱动视觉反馈。

## 3. 架构与设计约定

- **手机竖屏优先（mobile-first）**：默认样式针对窄屏优化；桌面适配通过单一 `@media (min-width: 720px)` 断点放大字号与内边距。
- **BEM 风格命名**：块（`card`、`agent`、`banner`、`controls`）+ 修饰（`.online`、`.offline`、`.danger`、`.ghost`、`.big`、`.hold-btn`）+ 子元素（`.agent-top`、`.agent-meta`、`.agent-actions`）。
- **状态驱动样式**：UI 状态以类名表达（`.pill.online/.offline`、`.agent.online/offline/bad-clock`、`.badge.q-excellent/q-good/q-poor/q-bad`），颜色由 CSS 变量统一映射，新增状态只需在 CSS 中追加规则。
- **可访问性与触控友好**：按钮启用 `touch-action: manipulation`、`user-select: none`；输入框固定 `font-size: 16px` 避免 iOS 聚焦缩放；使用 `env(safe-area-inset-bottom)` 适配刘海屏底部安全区。
- **视觉层次**：卡片阴影 `box-shadow: 0 1px 4px rgba(20,30,60,.06)`，主按钮带投影 `box-shadow: 0 4px 14px rgba(41,82,227,.35)`；左侧边框色（`border-left`）区分设备在线/离线/时钟偏差状态。
- **交互增强**：长按确认按钮通过 `.hold-fill` 绝对定位 + `transition: width 1s linear` 实现进度条动画，配合 JS 的 `.holding` 类触发。

## 4. 约定与约束

- **无第三方依赖**：不引入任何 CSS 框架或 UI 库，所有样式手写于单文件 `style.css`。
- **主题色集中管理**：所有颜色必须通过 `:root` 中的 CSS 变量引用，禁止在组件样式中硬编码十六进制色值（除 header 背景 `#16213e` 等少量品牌色外）。
- **语义化类名**：状态类（`.online`、`.offline`、`.bad-clock`、`.danger`、`.ghost`、`.primary`）需与 `app.js` 中动态添加/移除的逻辑一一对应，新增状态需同时更新 HTML 模板与 CSS。
- **响应式策略**：以 mobile-first 为基础，仅在 `720px` 及以上断点做渐进增强，不拆分多套样式表。
- **字体栈**：统一使用 `system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif`，数字列使用 `ui-monospace, Consolas, monospace` 保证等宽对齐。
- **无障碍基线**：保持 `color` 对比度满足 WCAG AA 级别（如 `.pill.offline` 使用 `#ffb4b4` 在深色背景上），错误信息使用 `.error` 类并以 `var(--bad)` 呈现。