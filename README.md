# Nekosol v0.1 使用教程网站

静态文档站：https://nekosol.929711.xyz/ 。包含 17 篇使用与维护文档，支持中性暗色 / 亮色主题、本地搜索、文档目录与代码复制，不包含首页 Canvas、流光或光晕效果。

本地预览：在此目录运行 `python -m http.server 8766 --bind 127.0.0.1`，访问 `http://127.0.0.1:8766/`。也可直接打开 index.html；剪贴板功能需要浏览器允许的安全上下文。

源码由 `main` 分支 `website/` 维护，GitHub Pages 从 `gh-pages` 根目录发布。同步本站文件时保留发布分支的 LICENSE，且必须包含 `.nojekyll` 与 `CNAME`（`nekosol.929711.xyz`）。当前未配置自动 Actions；源码提交不会自动同步到发布分支。

无数据库、聊天接口、外部字体或统计脚本；搜索本地完成，主题存 localStorage。`docs-data.js` 是网页正文源，与 `USER_GUIDE.zh-CN.md` 同步维护。仓库：https://github.com/2855680599/Nekosol ，下载以 Releases 为准；本站维护不重新验收运行时代码。
