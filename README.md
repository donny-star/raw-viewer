# RAW Viewer

Windows 桌面 RAW 图像预览器，支持紧凑打包的 Bayer `RAW8`、`RAW10` 和 `RAW12` 数据流。界面提供 1080p/4K 预设、帧选择、四种 Bayer 排列、拖入打开、PNG 导出和一键关闭文件。

> RAW10 与 RAW12 使用常见的 CSI-2 紧凑字节布局进行解包；界面格式名刻意简化为 RAW8 / RAW10 / RAW12。

## 功能

- 默认 `RAW10`，支持 `RAW8`、`RAW10`、`RAW12`
- Bayer 排列：GRBG、GBRG、RGGB、BGGR
- 默认“彩色直显”：仅进行 Bayer 去马赛克和位深缩放，不做白平衡、拉伸或 Gamma
- 左侧单一勾选项可统一开启自动白平衡、0.5%～99.5% 亮度拉伸和 Gamma 2.2
- 自动识别完整帧数量，支持多帧 RAW；尾部不完整帧会标注为“残帧”
- 文件拖放、`Ctrl+O` 打开、`Ctrl+S` 导出 PNG、左右方向键切帧、适应窗口 / 100% 缩放
- “关闭文件”会立即清空图像、帧状态和文件信息，并丢弃正在进行的后台解码结果

## 快速开始

需要 Python 3.10+。

```powershell
git clone https://github.com/donny-star/raw-viewer.git
cd raw-viewer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
raw-viewer
```

也可以直接指定文件：

```powershell
raw-viewer D:\dump\tmp\imx210.raw
```

## 使用方法

1. 拖入 RAW 文件，或点击左侧文件卡片。
2. 设置图像宽、高、Bayer 排列和格式。1920×1080 的 RAW10 是默认配置。
3. 通过帧滑条查看多帧数据。文件尺寸除以每帧字节数会自动得到完整帧数。
4. 默认查看彩色直显；如需更适合人眼观察的结果，勾选“启用白平衡 + 亮度拉伸 + Gamma”。
5. 点击“关闭文件”可回到无文件状态。

## 打包 EXE

安装开发依赖后，在 PowerShell 运行：

```powershell
.\build.ps1
```

输出为 `dist/RAWViewer.exe`。也可手动执行：

```powershell
python -m PyInstaller --noconfirm --clean --onefile --windowed --name RAWViewer --paths src src/raw_viewer/app.py
```

## 格式布局

| 格式 | 每组像素 | 字节数 | 宽度限制 |
| --- | ---: | ---: | --- |
| RAW8 | 1 | 1 | 无 |
| RAW10 | 4 | 5 | 4 的倍数 |
| RAW12 | 2 | 3 | 2 的倍数 |

RAW10 的前四个字节分别保存 4 个像素的高 8 位，第五个字节按从低到高顺序保存各像素的低 2 位。RAW12 的前两个字节保存两个像素的高 8 位，第三个字节的低/高半字节分别保存两个像素的低 4 位。

## 开发与测试

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
```

测试使用随机 RAW8/RAW10/RAW12 数据进行 pack/unpack 往返校验，不需要包含真实图像样本。真实 RAW 文件被 `.gitignore` 排除，避免误提交传感器数据。

## 项目结构

```text
src/raw_viewer/app.py    GUI、RAW 解包和去马赛克
tests/                   RAW8/10/12 往返测试
build.ps1                Windows 单文件 EXE 打包脚本
```
