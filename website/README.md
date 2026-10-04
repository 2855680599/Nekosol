# nyairo v0.1 使用教程网站

静态文档站：https://nyairo.com/ 。包含 17 篇使用与维护文档，支持中性暗色 / 亮色主题、本地搜索、文档目录与代码复制，不包含首页 Canvas、流光或光晕效果。

本地预览：在此目录运行 `python -m http.server 8766 --bind 127.0.0.1`，访问 `http://127.0.0.1:8766/`。也可直接打开 index.html；剪贴板写入被浏览器限制时，自动选中命令并提示手动复制。

源码由 `main` 分支 `website/` 维护，GitHub Pages 从 `gh-pages` 根目录发布。同步本站文件时保留发布分支的 LICENSE，且必须包含 `.nojekyll`；当前不带 `CNAME`，以 GitHub Pages 地址发布。GitHub Pages 会构建并部署发布分支；运行时功能 CI 模板尚未启用。main 源码提交不会自动同步到发布分支。

无数据库、聊天接口、外部字体或统计脚本；搜索本地完成，主题存 localStorage。`docs-data.js` 是网页正文源，与 `USER_GUIDE.zh-CN.md` 同步维护。仓库：https://github.com/2855680599/nyairo ，下载以 Releases 为准；本站维护不重新验收运行时代码。

修改脚本、文档正文或样式后，同步更新 index.html 中资源 URL 的 `?v=` 版本，避免访问者继续读取旧缓存。

自定义域名 `nyairo.929711.xyz` 目前尚未配置 DNS，不将其标成可用入口；配置完成后再恢复 CNAME 并核对 HTTPS。
