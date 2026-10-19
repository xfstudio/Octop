# 应用品牌资源

`musae-logo.png` 是用户提供的 108×108 透明 PNG 原文件，未重绘或改色。
SHA256：`67bde1566e151130413e07f8c47b36a24d235cff82d4ff5d3b8397a75a6d0342`。

在仓库根目录生成各平台素材：

```sh
uv run --no-sync python scripts/generate_branding.py
cd dashboard
npm run build
```

生成器覆盖网页、PWA、桌面程序、托盘、飞牛安装包、Linux 虚拟桌面和文档中的应用品牌标识。保留旧资源文件名作为兼容入口；已移除旧品牌动画。SVG 直接嵌入原 PNG，不伪称矢量重绘。尺寸较大的资源由该 108 像素原图等比缩放而来。

普通图标保留透明背景；PWA maskable 图标使用白底并把完整标志放入安全圆内；macOS 应用图标保留系统要求的边距，菜单栏模板按系统规范只使用原图的透明度轮廓。Windows ICO、macOS ICNS 和 Linux 构建图标属于忽略的生成文件，可在本地运行上述命令重建；各平台正式构建仍使用现有图标生成任务。

文档截图仅更新已知品牌区域；HTML 用户指南同步嵌入当前截图，截图色彩配置转换为 sRGB 后再放入标志。生成器同时重建不依赖旧标志的深色 Linux 壁纸。功能按钮、第三方服务图标、专家头像和用户上传内容保持各自语义；产品名称和许可证归属沿用 Octop。

开发工作集中在 `dev-musae`，`main` 用于跟随 `upstream/main`。`origin` 指向 `https://github.com/xfstudio/Octop.git`，`upstream` 指向 `https://github.com/TencentCloud/Octop.git`。
