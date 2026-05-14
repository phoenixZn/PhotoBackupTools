# Sync Win Tool

`sync_win_tool` 提供 Windows 图形化入口，用于调用现有的备份核心脚本，把手机目录按日期范围备份到电脑目录。

## 文件说明

- `run_backup_gui.py`：Tkinter 可视化启动器（主入口）
- `gui_config.json`：GUI 自动保存的最近一次配置
- `static_config.json`：手工维护的静态配置列表（用于快捷切换）

## 依赖要求

- Python 3.9+
- `adb` 已加入系统 PATH
- 手机已连接并开启 USB 调试，且完成授权
- 备份核心脚本存在：`cmd_adb_date_range_backup.py`

## 启动方式

在当前目录执行：

```bash
python run_backup_gui.py
```

或在仓库根目录执行：

```bash
python .\sync_win_tool\run_backup_gui.py
```

## 界面参数说明

- `Python Command`：Python 启动命令，默认 `python`
- `Phone Directory`：手机源目录，例如 `/sdcard/DCIM/Camera`
- `PC Directory`：电脑备份目标目录（支持按钮选择）
- `Start Date` / `End Date`：日期范围（闭区间，包含起止两天）
- `Include all files (--all-files)`：勾选后不限制扩展名；不勾选时仅备份常见媒体文件
- `Write sync record file (--write-sync-record)`：勾选后在目标目录同级写入/更新 `目标目录名_SyncRecord.txt`

## 校验与执行行为

- 必填项不能为空：Python 命令、手机目录、电脑目录、开始/结束日期
- 日期格式支持：
  - `YYYY.MM.DD`
  - `YYYY-MM-DD`
  - `YYYY/MM/DD`
- 开始日期不能晚于结束日期
- 点击 `Run Backup` 后会先弹窗确认，再执行备份
- 执行日志实时显示在窗口下方，结束后弹窗提示成功或失败

## 同步记录标记文件

- 启用 `Write sync record file (--write-sync-record)` 后，每次同步结束会合并更新同一个记录文件
- 路径规则：`<PC Directory 的父目录>/<PC Directory 名称>_SyncRecord.txt`
- 文件格式：标准 JSON（非 JSONL），并在 `history` 数组中追加本次记录
- `paramsSummary` 不包含目标目录参数（Target 目录）
- 若记录文件损坏，脚本会先尝试重命名为 `.bad.bak` 备份，再重建新记录

示例：

```json
{
  "targetFolderName": "Camera",
  "targetFolderPath": "E:\\PhotoBackup\\Camera",
  "latestFileTimeInFolder": "2026-04-29T20:15:32",
  "lastSyncDate": "2026-04-30T17:40:22.381204",
  "history": [
    {
      "commandDate": "2026-04-30T17:39:12.105930",
      "paramsSummary": {
        "phoneDir": "/sdcard/DCIM/Camera",
        "startDate": "2026-01-01",
        "endDate": "2026-12-31",
        "allFiles": false
      },
      "syncTimeRange": {
        "start": "2026-04-21T09:03:11",
        "end": "2026-04-29T20:15:32"
      },
      "syncFileCount": 124
    }
  ]
}
```

## 简单全量同步（外部 cmd）

- 界面新增 `简单全量同步` 按钮：点击后会新开一个 `cmd.exe` 窗口执行 `adb pull -a`
- 全量命令使用目录内容拉取：`adb pull -a "<phone_dir>/." "<pc_dir>"`，避免在目标目录下额外多一层同名目录
- 该模式不走 GUI 内部日志队列；GUI 仅负责参数确认、启动外部窗口、提示日志路径
- 外部窗口会在执行完成后保持打开，便于直接查看 `adb pull` 原生输出
- 日志仍会落盘到 `logs/`，文件名形如 `full_sync_<source>_<timestamp>.log`，内容为外部命令行执行结果摘要与原始输出

## 配置持久化

- 窗口关闭或点击运行时，会自动保存当前配置到 `gui_config.json`
- 下次打开窗口会自动回填上一次配置
- `static_config.json` 仅用于手工维护多组预设，程序运行时不会自动写回

## 静态配置快捷切换

- 界面中的 `Static Config` 下拉框可快速将某一组静态配置填充到表单
- 点选静态配置不会自动执行备份；仍需点击 `Run Backup`
- 点选后若执行备份或关闭窗口，当前表单值仍会按原逻辑保存到 `gui_config.json`

`static_config.json` 结构示例（列表中的每项都包含 `name`）：

```json
[
  {
    "name": "Camera-2026",
    "python": "python",
    "phone_dir": "/sdcard/DCIM/Camera",
    "pc_dir": "E:\\PhotoBackup\\Camera",
    "start_date": "2026.01.01",
    "end_date": "2026.12.31",
    "all_files": false,
    "write_sync_record": false
  }
]
```

## 常见问题

- 提示找不到核心脚本：确认 `cmd_adb_date_range_backup.py` 文件仍在仓库中
- 提示 ADB 设备未就绪：检查 USB 连接、调试授权、`adb devices` 输出
- 执行失败返回非 0：查看日志区域中的错误输出定位原因
