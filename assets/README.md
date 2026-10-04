# nyairo 品牌图

`nyairo-icon.png` 为猫与聊天气泡融合的图形标志，`nyairo-horizontal.png` 为图形与小写 nyairo 的横向组合。两份都是**透明背景** PNG，可直接叠在任意底色上，网页里不再需要白色垫底。

`nyairo-icon-light.png`、`nyairo-horizontal-light.png` 是同一形状的浅色版本（#AFC6E4），供深色背景使用；网页在深色主题下自动切换到它们。

`favicon.ico`（含 16/32/48/64）、`favicon-16.png`、`favicon-32.png`、`apple-touch-icon.png`（180×180，深色底）由 `nyairo-icon.png` 生成。

原始素材是白底 PNG（通过 image_gen 创建／编辑，不是 SVG 矢量源文件）。透明版按「白色转透明」处理：用最小通道估算 alpha 并反预乘，保留抗锯齿边缘；同时把所有像素统一为品牌蓝 #54729A，去掉原始生成图的颜色噪点。小尺寸图标在缩放前做过描边加粗，避免 16px 下线条糊掉。

以项目根目录 Apache-2.0 许可分发，使用时不要暗示获得作者背书。
