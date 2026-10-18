# RecycleBinGuard 真实清空验收手册（T5 人工部分）

> 本手册只覆盖 **T5 中无法自动化**的 3 项：真实清空、长期运行、干净环境安装/卸载。
> 其余 10 项已由自动化测试覆盖（见 `README.md`「阶段状态」与 `PRD.md` 第 14 节）。
>
> **本手册中的操作会真的删除文件。请在虚拟机或专用 Windows 用户下执行，不要在日常电脑上做真实清空测试。**

---

## 0. 必读风险

- 程序一旦触发，会清空**所有本机固定磁盘**的回收站，包括你手动删除、可能还想恢复的文件；
- 本手册要求你在**专用环境**执行，并在开始前创建快照 / 系统还原点；
- 程序**不需要管理员权限**；除自动升级检查（GitHub Releases）外不联网、不上传任何数据；
- 自动化测试永远不会执行真实清空；真实清空只在你按第 4 章在专用环境手动触发时发生。

### 安全网（默认开启，但每次都经你确认）

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `auto_clean_enabled` | `true` | 默认开启自动清理；但每次清理前都会弹确认框 |
| `confirm_before_clean` | `true` | 达到阈值后先弹确认窗；取消 / 关闭 / 倒计时归零进复核都不直接清空 |
| `auto_clean_confirmed` | `false` | 关闭后再启用自动清理前，需确认一次不可逆删除风险 |

**程序默认就会自动清理，但每一次真实清空前都会弹确认框；把 `auto_clean_enabled` 设为 `false` 即回到“只监控、不清空”。** `dry_run` 试运行模式已移除。

---

## 1. 前置条件

1. Windows 10 x64（虚拟机最佳；若用物理机，务必换一个专用 Windows 用户）；
2. 已创建快照 / 系统还原点；
3. 回收站中的文件**全部可丢弃**；
4. 准备一个 U 盘（用于验证“可移动盘不被清空”这一安全设计）。

记录基线（后面要对照）：

```powershell
# 当前回收站项目数（Shell 口径）
$shell = New-Object -ComObject Shell.Application
$shell.Namespace(0xA).Items().Count
```

---

## 2. 阶段 A：只读基线（不会删除任何东西）

### 打开入口自检（先确认你能找到程序）

```powershell
RecycleBinGuard.exe --install-shortcuts
```

- [ ] 桌面出现 `RecycleBinGuard` 快捷方式；
- [ ] 开始菜单出现 `RecycleBinGuard` 组；
- [ ] 双击快捷方式 → 状态窗口**直接弹出**（不是只在托盘里）；
- [ ] 再双击一次 → 已有窗口被**重新显示**出来（不会弹“已运行”提示，也不会出现第二个进程）；
- [ ] 按 `Ctrl + Alt + R` → 窗口随时唤出（托盘菜单里可关闭该热键）。


```powershell
# 源码运行
python -m recyclebingguard --check-once

# 或使用打包后的 EXE（无控制台，用 --report 落盘）
& "D:\RecycleBinGuard\dist\RecycleBinGuard.exe" --check-once --report "$env:USERPROFILE\Desktop\rbg-check.txt"
Get-Content "$env:USERPROFILE\Desktop\rbg-check.txt"
```

**通过标准**

- [ ] 输出中 `统计完整 : True`；
- [ ] `项目总数` 与第 1 步 Shell 口径一致（逐盘之和）；
- [ ] 每个固定磁盘单独列出（`C:\ D:\ E:\ ...`）；
- [ ] 全程回收站项目数没有任何变化。

---

## 3. 阶段 B：只监控模式触发验证（`auto_clean_enabled=false`，不会删除任何东西）

目的：在**关闭自动清理**的前提下，验证「达到阈值 → 记录会触发、但不清理」这条链路。

> **不要为了测试真的往回收站塞 10,000 个文件**，把阈值临时调小即可。

### 3.1 写入配置

```powershell
python -m recyclebingguard --init-config
notepad ".\config.json"          # 程序（EXE）所在目录，即当前工作目录
```

改成：

```json
{
  "threshold_items": 3,
  "poll_interval_seconds": 10,
  "auto_clean_enabled": false
}
```

`auto_clean_enabled: false` 是关键——即使达到阈值也**只会记录、不会**调用清空 API（这就是旧的“试运行”等效模式）。

### 3.2 制造 3 个以上的回收站项目

```powershell
$dir = "$env:TEMP\rbg-test"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
1..5 | ForEach-Object { "test $_" | Out-File "$dir\f$_.txt" }
# 移到回收站（不是彻底删除）
Add-Type -AssemblyName Microsoft.VisualBasic
Get-ChildItem $dir | ForEach-Object {
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
    $_.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
}
```

### 3.3 观察

```powershell
Get-Content ".\logs\recyclebingguard.log" -Tail 20 -Wait
```

**通过标准**

- [ ] 出现 `达到阈值：项目数=5 阈值=3`；
- [ ] 自动清理关闭，达到阈值后**仅记录、不弹确认框、不清理**；
- [ ] 状态在 `idle -> checking -> threshold_reached -> idle` 之间迁移；
- [ ] **回收站项目数没有变化**（再用第 1 步的 Shell 口径确认，仍是 5 个左右）。

---

## 4. 阶段 C：真实清空（专用环境，会真的删除）

> 执行到这一步之前，请再确认：这里是虚拟机/专用用户，且回收站里的东西你都不想要了。

### 4.1 准备可辨识的测试文件

```powershell
$dir = "$env:TEMP\rbg-real"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
1..6 | ForEach-Object { "real test $_" | Out-File "$dir\g$_.txt" }

# 同时把一个 U 盘上的文件也丢进回收站，用来验证“可移动盘不被清空”
# （把 X: 换成你的 U 盘盘符，U 盘上的回收站项目是本次的关键反例）
Add-Type -AssemblyName Microsoft.VisualBasic
Get-ChildItem $dir | ForEach-Object {
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
    $_.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
}
if (Test-Path "X:\") {
  "usb" | Out-File "X:\rbg-usb.txt"
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
    "X:\rbg-usb.txt", 'OnlyErrorDialogs', 'SendToRecycleBin')
}

# 再留一个“不在回收站里的哨兵文件”，确认不会被误删
"sentinel" | Out-File "$env:USERPROFILE\Desktop\rbg-sentinel.txt"
```

### 4.2 开启自动清理（默认即为开启）

把 `config.json` 改成：

```json
{
  "threshold_items": 3,
  "poll_interval_seconds": 10,
  "auto_clean_enabled": true,
  "auto_clean_confirmed": true
}
```

然后启动程序（双击 EXE，或 `python -m recyclebingguard`），托盘会出现图标。

### 4.3 观察清理过程

```powershell
$log = ".\logs\recyclebingguard.log"
Get-Content $log -Tail 30 -Wait
```

**通过标准（对应 PRD T5 第 11–13 项）**

- [ ] **11** 只在本次专用环境执行；日志中没有出现网络、提权、用户目录相关动作；
- [ ] 出现 `达到阈值：项目数=N 阈值=3`；
- [ ] 出现 `开始清空回收站：复核总数=N 目标磁盘=['C:\\', 'D:\\', ...]`；
- [ ] 出现逐盘 `清空磁盘回收站：磁盘=C:\ HRESULT=S_OK`；
- [ ] **12** 出现 `回收站清空成功：清理前=N 耗时=...s`，且随后 `回收站统计：总数=0`；
- [ ] 用第 1 步 Shell 口径复核，项目数为 **0**；
- [ ] **13** 桌面上的 `rbg-sentinel.txt` **仍然存在**；
- [ ] **13** U 盘（可移动盘）的回收站**未被清空**——这是刻意的安全设计：
      清空范围严格等于统计范围（仅本机固定磁盘）；
- [ ] `%TEMP%\rbg-real` 之外的个人文件没有任何变化。

### 4.4 验证“统计不完整则不清理”（可选但强烈建议）

1. 保持 `auto_clean_enabled: true`、阈值 3；
2. 用管理员权限打开一个句柄占住某个盘的回收站，或临时把某块固定磁盘拔掉/禁用；
3. 重启程序，确认日志出现 `统计不完整，本轮不做任何清理`，且**没有** `开始清空回收站` 行。

**通过标准**

- [ ] 统计不完整时程序选择不清理，而不是把未知磁盘当成 0。

---

## 5. 阶段 D：收尾

```powershell
# 1) 立即关闭自动清理（托盘右键 → 关闭自动清理，或直接改配置）
notepad ".\config.json"   # auto_clean_enabled 改回 false

# 2) 关闭开机启动
& "...\RecycleBinGuard.exe" --autostart off

# 3) 移除快捷方式
RecycleBinGuard.exe --remove-shortcuts
#    （或 RecycleBinGuard.exe --uninstall 会同时移除开机启动项 + 快捷方式）

# 4) 退出程序（托盘右键 → 退出程序），确认任务管理器中没有残留进程
Get-Process RecycleBinGuard -ErrorAction SilentlyContinue

# 4) 清掉测试产生的哨兵文件
Remove-Item "$env:USERPROFILE\Desktop\rbg-sentinel.txt" -ErrorAction SilentlyContinue
```

**恢复日常使用**：确认 `auto_clean_enabled: true`、`confirm_before_clean: true`，
再把 `threshold_items` 改回 `10000`、`poll_interval_seconds` 改回 `600`。

---

## 6. 长期运行验证（T5 第 9 项）

在**只监控、不清空**（`auto_clean_enabled: false`）的状态下挂着跑 3–7 天：

```powershell
# 每 10 分钟采样一次内存与句柄
while ($true) {
  $p = Get-Process RecycleBinGuard -ErrorAction SilentlyContinue
  if ($p) { "{0} 内存={1:N1}MB 句柄={2} 线程={3}" -f (Get-Date -f HH:mm:ss), ($p.WorkingSet64/1MB), $p.HandleCount, $p.Threads.Count }
  Start-Sleep -Seconds 600
}
```

**通过标准**

- [ ] 内存不持续增长（在 ±20 MB 内波动，无单调上升）；
- [ ] 句柄数不持续增长；
- [ ] 日志按 1 MiB × 5 份轮转，目录不会无限膨胀；
- [ ] 托盘图标、菜单、状态窗口始终可响应。

---

## 7. 干净环境安装 / 卸载（T5 第 10 项）

在**没有安装 Python** 的干净 Windows 10 x64 中：

- [ ] 把 `dist\RecycleBinGuard.exe` 单个文件拷过去，双击能启动；
- [ ] 托盘出现图标，状态窗口能打开；
- [ ] 重复双击 → 弹出「已在运行」提示，任务管理器中**只有一个**进程；
- [ ] 勾选「开机启动设置」→ 注销并重新登录 → 托盘图标自动出现；
- [ ] 取消勾选 → 注销重新登录 → 不再自动启动；
      （也可用「任务管理器 → 启动」核对，或在注册表
      `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中核对）
- [ ] 任务管理器中强杀进程 → 再次双击启动 → **能正常启动**（不会卡在“已在运行”）；
- [ ] 运行 `RecycleBinGuard.exe --uninstall` → 启动项被移除，配置与日志**保留**；
- [ ] 运行 `RecycleBinGuard.exe --uninstall --purge` → 配置与日志一并删除；
- [ ] 卸载后注销重新登录，不再自动启动。

---

## 8. 故障排查

| 现象 | 排查 |
| --- | --- |
| 程序启动后没有任何反应 | 看**程序所在目录**下的 `logs\recyclebingguard.log`；若文件都不存在，看同目录 `crash.log` |
| 双击无反应且疑似已在运行 | 任务管理器结束 `RecycleBinGuard.exe`；锁由操作系统持有，强杀后会自动释放 |
| 状态显示「统计不完整」 | 查看日志中 `回收站统计失败` 行的磁盘与 HRESULT；修复该磁盘后会自动恢复正常 |
| 托盘提示「清理失败 / 部分清理」 | 日志中 `清理未成功：outcome=partial|failed|incomplete`；程序会退避 300 秒再试，**不会**在同一轮无限重试 |
| 想确认程序到底会不会删东西 | 把 `auto_clean_enabled` 设为 `false` 观察日志，达到阈值只会记录、不会出现「开始清空回收站」 |
| 想看程序统计了什么 | `RecycleBinGuard.exe --check-once --report out.txt`，然后打开 `out.txt` |

日志中**永远不会**出现文件内容、文件名清单或用户目录扫描结果；
只有时间、阈值、统计数字、HRESULT 与错误信息。

---

## 9. 验收结论模板

```
环境：□ 虚拟机 / □ 专用 Windows 用户        日期：__________
阶段 A 只读基线           : □ 通过  □ 不通过
阶段 B 只监控触发         : □ 通过  □ 不通过
阶段 C 真实清空           : □ 通过  □ 不通过
阶段 D 收尾与恢复         : □ 通过  □ 不通过
统计不完整不清理          : □ 通过  □ 未验证
可移动盘未被误清          : □ 通过  □ 不通过
长期运行资源稳定          : □ 通过  □ 未验证（运行 __ 小时）
干净环境安装/卸载         : □ 通过  □ 不通过
备注：________________________________________________
决策：□ 可在日常电脑保持自动清理开启   □ 关闭自动清理只监控
```
