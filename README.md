# nyairo v0.1 使用教程网站

静态文档站：https://nyairo.com/ 。包含 17 篇使用与维护文档，支持中性暗色 / 亮色主题、本地搜索、文档目录与代码复制，首页用蓝色标题强调框架的连续性与记忆，并分别介绍记忆、生活、世界身体、认知、文档和数据边界；标题的轻微流光尊重减少动画设置。

本地预览：在此目录运行 `python -m http.server 8766 --bind 127.0.0.1`，访问 `http://127.0.0.1:8766/`。也可直接打开 index.html；剪贴板写入被浏览器限制时，自动选中命令并提示手动复制。

源码由 `main` 分支 `website/` 维护，GitHub Pages 从 `gh-pages` 根目录发布。同步本站文件时保留发布分支的 LICENSE，且必须包含 `.nojekyll` 与 `CNAME`，CNAME 内容为 `nyairo.com`。GitHub Pages 会构建并部署发布分支；运行时功能 CI 模板尚未启用。main 源码提交不会自动同步到发布分支。

无数据库、聊天接口、外部字体或统计脚本；搜索本地完成，主题存 localStorage。`docs-data.js` 是网页正文源，与 `USER_GUIDE.zh-CN.md` 同步维护。仓库：https://github.com/L1AN929/nyairo ，下载以 Releases 为准；本站维护不重新验收运行时代码。

修改脚本、文档正文或样式后，同步更新 index.html 中资源 URL 的 `?v=` 版本，避免访问者继续读取旧缓存。

文档域名为 `nyairo.com`，`www.nyairo.com` 自动 301 跳转到它，HTTPS 已启用。发布分支的 `CNAME` 必须写 `nyairo.com`；写成别的域名会让线上站点失效。

官网 `/install.sh` 与 `scripts/bootstrap.sh` 内容完全相同，提供 Linux / WSL 引导安装，固定下载并校验 rc8。更新时同步这两个脚本；网站仍是静态教程站，模型设置和聊天在用户电脑上的 Hermes 中进行。
