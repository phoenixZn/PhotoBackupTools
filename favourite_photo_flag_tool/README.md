# Favourite Photo Flagger

一个基于 `tkinter` 的本地照片浏览与喜爱标记工具，适用于 Windows。  
你可以快速逐张预览目录中的照片，并实时记录喜爱清单，方便后续将喜爱照片同步到 Android 设备。

## 功能

- 通过 GUI 选择要浏览的照片目录
- 高效逐张预览（按窗口自动缩放）
- 支持按钮和键盘快捷键进行翻页、喜爱标记/取消
- 实时保存喜爱清单到 `favourite_list.txt`
- 实时保存当前浏览位置到 `preview_pos.txt`
- 显示图片基础信息：文件名、序号、分辨率、文件大小、是否喜爱

## 环境要求

- Python 3.10+
- Pillow

## 安装依赖

```bash
pip install pillow
```

## 运行

在项目目录下执行：

```bash
python photo_flagger.py
```

## 使用说明

1. 启动程序后，点击“选择目录”选择照片目录。
2. 程序会扫描该目录及其子目录中的图片文件（`jpg/jpeg/png/bmp/webp/gif`）。
3. 使用按钮或快捷键翻阅和标记喜爱。
4. 每次标记变化都会立即写入 `favourite_list.txt`。
5. 每次切换图片都会立即写入 `preview_pos.txt`，下次打开可续看。

## 快捷键

- `Left` / `A`：上一张
- `Right` / `D`：下一张
- `F`：切换喜爱（有则取消，无则添加）
- `Up` / `W`：添加喜爱
- `Down` / `S`：移除喜爱
- `Home`：跳到第一张
- `End`：跳到最后一张

## 输出文件格式

两个文件都保存在“当前浏览目录”下：

- `favourite_list.txt`  
  - UTF-8 编码
  - 每行一条相对路径（相对于浏览目录）
- `preview_pos.txt`  
  - UTF-8 编码
  - 仅一行，记录当前图片的相对路径

## 验收清单

- 可正常选择目录并加载图片
- 可正常通过按钮和键盘翻阅
- 标记/取消喜爱后，`favourite_list.txt` 实时更新
- 切换图片后，`preview_pos.txt` 实时更新
- 重启程序后，能恢复到上次浏览位置
- 无图片目录下有明确提示且不会报错
