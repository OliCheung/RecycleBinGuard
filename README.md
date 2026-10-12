# RecycleBinGuard

Windows 10 回收站后台监控与自动清理工具（第一版）。

> 需求基线见 [PRD.md](./PRD.md)。本仓库按 **T1 → T2 → T3 → T4 → T5** 分阶段实现，当前处于**第一阶段（项目骨架）**。

## 它做什么

后台每 180 秒检查一次 Windows 回收站项目总数，当总数达到 10,000 时自动清空整个回收站。用户明确启用自动清理后，无需逐次确认。

## 它不做什么

- 不做网络请求、遥测、大模型调用；
- 不做数据库 / Web 界面 / 云同步；
- 不直接遍历或删除 `$Recycle.Bin` 下的 `$I` / `$R` 文件；
- 默认不需要管理员权限。

## 安全默认值

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `auto_clean_enabled` | `false` | 首次安装只监控，不自动清理 |
| `auto_clean_confirmed` | `false` | 是否已确认过不可逆删除风险（首次启用前必须确认） |
| `dry_run` | `true` | 试运行，只记录“本会触发清理”，绝不调用清空 API |
| `confirm_before_clean` | `true` | 达到阈值后先弹确认窗；关闭则回到“无需逐次确认”的自动清理 |
| `confirmation_timeout_seconds` | `10` | 确认窗倒计时：`>0` 归零后进入复核并清理；`0` 不自动，一直等用户 |
| `threshold_items` | `10000` | 触发阈值 |
| `poll_interval_seconds` | `180` | 轮询间隔 |
| `failure_backoff_seconds` | `300` | 失败退避 |
| `start_on_login` | `false` | 登录后自动启动（静默驻留托盘） |
| `hotkey_enabled` | `true` | 全局热键 Ctrl + Alt + R 唤出状态窗口 |

> 统计不完整（任一固定磁盘查询失败）时，程序**选择不清理**，绝不把未知磁盘当成 0。

## 目录结构

```
RecycleBinGuard/
├── PRD.md                     # 产品需求文档（需求基线）
├── README.md
├── ACCEPTANCE.md              # 真实清空人工验收手册（T5）
├── build.py                   # 一键构建（测试 → 图标 → PyInstaller → 冒烟）
├── requirements.txt           # 运行依赖
├── requirements-dev.txt       # 开发/测试/打包依赖
├── pyproject.toml             # 打包与 pytest 配置
├── config.example.json        # 配置示例
├── packaging/
│   ├── launcher.py            # PyInstaller 入口（绝对导入，避免相对导入失败）
│   └── RecycleBinGuard.spec   # PyInstaller 目录式构建配置
├── tools/
│   └── make_icon.py           # 由托盘图标生成 packaging/recyclebingguard.ico
├── recyclebingguard/          # 主包
│   ├── __init__.py
│   ├── __main__.py            # 入口：单实例、日志、托盘启动、卸载/自启命令
│   ├── paths.py               # 应用数据目录 / 日志 / 锁文件路径
│   ├── config.py              # 配置模型与读写（默认安全配置）
│   ├── logging_setup.py       # 轮转日志
│   ├── interfaces.py          # T1/T2 核心接口与数据类型定义
│   ├── recycle_bin_service.py # T1：回收站统计与清空核心（Shell API）
│   ├── recycle_bin_monitor.py # T2：阈值监控与安全状态机
│   ├── confirmation.py        # 清理前确认的请求 / 决策契约（与 UI 无关）
│   ├── confirm_dialog.py      # 清理前确认弹窗（tkinter，10 秒倒计时）
│   ├── single_instance.py     # T3：操作系统级单实例锁 + 唤醒已有实例
│   ├── app.py                 # T3：应用控制器（与 UI 无关）
│   ├── tray.py                # T3：pystray 托盘 + tkinter 状态窗口
│   ├── hotkey.py              # T3：全局热键 Ctrl+Alt+R（RegisterHotKey）
│   ├── shortcuts.py           # T3：开始菜单 / 桌面快捷方式
│   └── autostart.py           # T4：当前用户登录启动项
└── tests/                     # pytest 测试
```

## 环境准备

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

## 运行测试

```powershell
python -m pytest -q
```

## 数据与日志位置

- 配置：`%LOCALAPPDATA%\RecycleBinGuard\config.json`
- 日志：`%LOCALAPPDATA%\RecycleBinGuard\logs\recyclebingguard.log`
- 单实例锁：`%LOCALAPPDATA%\RecycleBinGuard\RecycleBinGuard.lock`
- 清理历史：`%LOCALAPPDATA%\RecycleBinGuard\clean_history.json`

> **清理历史是什么**：记录「每一次真正调用过清空 API 的清理」（成功 / 部分 / 失败 / 异常都记，含时间、清理前后数量、触发方式、耗时、错误信息），程序重启后仍可查询。轮转日志会被覆盖、`last_clean_result` 只存内存，因此单独用结构化 JSON 文件持久化「上一次达到阈值并执行清理是什么时候」。详细说明见 T2 的「清理历史」小节。

测试时可通过环境变量 `RECYCLEBINGUARD_HOME` 覆盖基准目录，避免污染真实用户数据。

## T1：回收站统计与清空核心（`recyclebingguard/recycle_bin_service.py`）

### 使用的 Windows API

| API | 用途 |
| --- | --- |
| `SHQueryRecycleBinW` | 查询**单个盘符**回收站的项目数（`i64NumItems`）与占用字节（`i64Size`） |
| `SHEmptyRecycleBinW` | 清空**指定盘符**的回收站 |
| `GetLogicalDrives` / `GetDriveTypeW` | 枚举盘符并判定磁盘类型 |

清空标志固定为 `SHERB_NOCONFIRMATION \| SHERB_NOPROGRESSUI`（不弹系统确认框、不显示进度对话框），
并使用 `SHERB_NOSOUND` 之外的默认行为；刻意**不使用任何“静默错误”式标志**，错误由本模块显式处理与上报。

### 统计口径

- 逐盘查询后求和，单盘计数取 `SHQUERYRBINFO.i64NumItems`；
- 只统计**本机固定磁盘**（`DRIVE_FIXED`）；可移动盘 / 网络盘 / 光驱 / 内存盘 / 未挂载卷不纳入；
- 不使用 `$Recycle.Bin` 目录内 `$I` / `$R` 的文件数量；
- 结果同时包含总数、成功磁盘、失败磁盘、查询时间与每个磁盘的明细；
- **任一应纳入磁盘查询失败，或一个固定磁盘都没枚举到** → `is_complete == False`，
  `should_trigger()` 恒为 `False`，绝不会把未知磁盘当成 0。

### 清空行为与安全边界

1. **清空前二次核验**：复核统计不完整时直接返回 `INCOMPLETE`，**不调用**清空 API；
2. **逐盘清空**：对每个已纳入统计的固定磁盘调用 `SHEmptyRecycleBinW(root)`，
   刻意**不传 `NULL`**（`NULL` 会波及本次未统计的可移动盘与网络盘）；
3. **清空后复核**：重新统计并以此为准判定结果；
4. 结果类型：

| 结果 | 触发条件 |
| --- | --- |
| `SUCCESS` | 复核后总数为 0 |
| `PARTIAL` | 部分磁盘清空成功，复核后仍有项目（**不重复清空**） |
| `FAILED` | 所有磁盘清空调用均失败，且复核后仍有项目 |
| `INCOMPLETE` | 清空前或清空后统计不完整，结果未确认 |

`CleanResult` 同时返回 `hresult`、`duration_seconds`、`items_before`、`items_after`、`remaining_items` 与 `error`。

### 已知限制

- 仅支持盘符（`C:\` 等）；通过文件夹挂载的卷（volume mount point）不纳入；
- 计数来自 Shell 接口，与资源管理器“回收站属性”同一来源，但不保证与界面上每一种统计口径完全一致；
- 不保证清空耗时——Windows 原生清空速度受文件数量、磁盘性能与文件系统状态影响；
- 本模块不含定时器、托盘 UI 与开机启动逻辑（分属 T2 / T3 / T4）。

### 测试方式

```powershell
python -m pytest tests/test_recycle_bin_service.py -q
```

- **默认全部使用 fake API**（`FakeShellApi`）隔离 Windows API，可在任何平台运行；
- 覆盖：多盘求和、可移动/网络盘排除、单盘失败、负数计数、无固定磁盘、
  API 错误、清空失败 / 部分失败 / 复核失败、清空后不重复重试、标志位与结构体布局；
- **真实 API 仅做只读验证**（`test_real_api_readonly_scan`）：枚举磁盘 + `scan()`，
  **绝不执行真实清空**，也绝不会清空开发者当前的真实回收站。

真实统计交叉核验示例（只读，可复现）：

```powershell
# 本工具
python -c "from recyclebingguard.recycle_bin_service import RecycleBinService; print(RecycleBinService().scan().total_items)"
# 独立口径：Shell.Application 回收站命名空间
$shell = New-Object -ComObject Shell.Application; $shell.Namespace(0xA).Items().Count
```

## T2：阈值监控与安全状态机（`recyclebingguard/recycle_bin_monitor.py`）

### 默认安全配置

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `threshold_items` | `10000` | 触发阈值 |
| `poll_interval_seconds` | `180` | 轮询间隔 |
| `auto_clean_enabled` | `false` | **首次安装默认关闭**，只监控 |
| `dry_run` | `true` | **首次安装默认开启**，只记录不清理 |
| `confirm_before_clean` | `true` | 达到阈值后先弹确认窗（见下节） |
| `confirmation_timeout_seconds` | `10` | 确认窗倒计时秒数，`0` = 不自动 |
| `failure_backoff_seconds` | `300` | 失败退避 |

### 触发流程

1. 每 `poll_interval_seconds` 秒调用一次 `service.scan()`；
2. 统计不完整（`is_complete == False`）→ 只记录，本轮不做任何清理、**不弹窗**；
3. `total_items < threshold` → 继续监控；
4. `total_items >= threshold` → 状态 `THRESHOLD_REACHED`；
5. 若自动清理未启用或已暂停 → 仅记录；
6. 若处于失败退避期 → 等待退避结束；
7. 若 `dry_run` → 记录“本次会触发清理”，**绝不调用清空 API**；
8. 若 `confirm_before_clean` → 状态 `AWAITING_CONFIRMATION`，请求确认弹窗（**监控线程不等待**）：
   - 取消 / 关闭窗口 / 程序退出 / 暂停 → 本轮放弃，清理 API 调用次数为 0；
   - 倒计时归零或点击「立即清理」→ **重新统计复核**；
   - 复核不完整或未达阈值 → 放弃本轮并记录原因；
9. **复核通过** → 状态 `CLEANING` → 调用 `service.empty()`（内部含清空前复核与清空后复核）；
10. 状态 `VERIFYING` → 再次统计并记录；
11. 清理成功 → `IDLE`；非 `SUCCESS`（`PARTIAL` / `FAILED` / `INCOMPLETE`）→ `ERROR` 并退避 300 秒；
12. 下一轮仍达阈值时可以再次弹窗。

### 清理前确认弹窗（`confirmation.py` + `confirm_dialog.py`）

| 项 | 行为 |
| --- | --- |
| 标题 | `RecycleBinGuard — 回收站清理确认` |
| 内容 | 当前项目数、自动清理阈值、永久删除警告、倒计时（“10 秒后自动清空回收站”） |
| 按钮 | 「立即清理」/「取消本次清理」；**没有默认按钮**，回车不会确认 |
| 关闭窗口 | 等同取消 |
| 倒计时 | `after()` 每秒刷新 `10…1`，归零后进入复核流程（不是直接清理） |
| `confirmation_timeout_seconds = 0` | 不倒计时，弹窗一直等用户 |
| 同一时刻 | 最多一个弹窗、一个待确认请求、一个清理任务 |
| 线程 | 弹窗只在 UI 主线程创建与更新；复核与清理在后台线程执行 |

每次请求带一个单调递增的 `token`：**过期、重复、取消后、退出后、暂停后**的决定一律丢弃，
绝不产生“残留任务仍然清理”或“点一次按钮清理两次”。

### 状态机

`IDLE` → `CHECKING` → `THRESHOLD_REACHED` → `AWAITING_CONFIRMATION` → `CLEANING` →
`VERIFYING` →（`IDLE` / `ERROR`），任意时刻可通过 `pause()` 进入 `PAUSED`，`resume()` 回到 `IDLE`。

### 并发与残留任务

- `tick()` / `_clean()` 均在可重入锁内检查 `_cleaning` 标志，**同一时刻只有一个清理任务**；
- 监控只有一个后台线程，不存在“队列”，因此暂停或关闭自动清理后**不会执行残留清理**；
- `_clean()` 在真正调用 `empty()` 前会再次检查 `PAUSED` 与 `auto_clean_enabled`；
- 后台线程内的任何异常都会被捕获、记入 `last_error`、进入 `ERROR` 并退避，**不会静默吞掉**。

### 编程接口

```python
from recyclebingguard.config import load_config
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor
from recyclebingguard.recycle_bin_service import RecycleBinService

monitor = RecycleBinMonitor(RecycleBinService(), load_config())
monitor.start()            # 后台轮询
monitor.status()           # MonitorStatus 快照，供 UI 使用
monitor.check_now()        # 立即执行一次检查
monitor.pause() / resume()
monitor.set_auto_clean_enabled(True)   # 关闭后本轮的清理会被拒绝
monitor.set_dry_run(False)
monitor.stop()             # 停止并回收线程
```

### 测试方式

```powershell
python -m pytest tests/test_recycle_bin_monitor.py tests/test_confirmation.py -q
```

- 使用 fake 服务 + 假时钟，不等待真实时间，也**不触碰真实回收站**；
- 覆盖：9999 / 10000 / 10001 边界、统计不完整不触发、dry-run 不调用清空、
  自动清理关闭 / 暂停不清理、二次核验取消清理、清理期间不并发、
  失败退避后才重试、同一轮不重复清理、成功后重新统计、异常不静默、
  线程启动 / 停止 / 重复启动 / 幂等停止。

`tests/test_confirmation.py` 另外覆盖清理前确认弹窗：9999 不弹 / 10000、10001 弹、
确认只清理一次、取消 / 关闭 / 复核失败 / 复核低于阈值 / dry-run 一律 0 次清理、
两个周期最多一个弹窗、退出或暂停后过期决定不清理、倒计时逐秒更新与归零进复核、
配置为 0 或非法值不自动清理、弹窗只用 `after()` 且不阻塞 UI。

### 清理历史（记录「上一次执行清理是什么时候」）

每次**真正调用了清空 API** 的清理，都会被追加写入
`%LOCALAPPDATA%\RecycleBinGuard\clean_history.json`（与轮转日志分离，便于跨重启回答“上一次清理是什么时候”）：

- 记录字段：完成时间、结果类型（`success`/`partial`/`failed`/`incomplete`）、触发方式
  （`confirmed`=点立即清理 / `timeout`=倒计时归零 / `auto`=未开弹窗的自动 / `error`=清理中异常）、
  清理前 / 后数量、触发阈值、耗时、错误信息；
- **不记录任何文件名或文件内容**；
- **不写入**的情形：试运行（dry-run）、取消确认、复核未通过、清理未真正调用 API；
- 文件损坏 / 格式非法时返回空历史并记日志，**不抛异常、不影响监控**；
- 原子写入（先写 `.tmp` 再 `replace`），最多保留 `MAX_RECORDS=50` 条；
- 可通过 `AppController.last_clean_summary()` / `recent_cleans()` 查询；状态窗口新增「上次清理」行、托盘菜单新增「查看清理历史」。

```python
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor

monitor = RecycleBinMonitor(RecycleBinService(), load_config())
print(monitor.last_clean_record())          # 最近一次（含失败），首次从磁盘加载
print(monitor.last_successful_clean())      # 最近一次成功
```

相关测试见 `tests/test_clean_history.py`（覆盖记录往返、成功 / 失败 / 异常均落盘、
dry-run / 取消 / 复核失败不落盘、跨重启可读回、文件损坏降级、写入失败返回 False 等）。

## T3：系统托盘与状态窗口

模块：`single_instance.py`（锁 + 唤醒）、`app.py`（控制器）、`tray.py`（界面）、
`hotkey.py`（全局热键）、`shortcuts.py`（快捷方式）、`__main__.py`（装配）。

### 打开方式（四种入口）

Windows 10 的托盘图标经常被折叠进“隐藏的图标”区域，因此程序提供四种确定的打开方式：

| 入口 | 说明 |
| --- | --- |
| **桌面 / 开始菜单快捷方式** | `--install-shortcuts` 创建；双击即启动，或唤醒已有实例 |
| **全局热键 `Ctrl + Alt + R`** | 程序运行时随时唤出状态窗口，可在托盘菜单里关闭（默认开启） |
| **重复启动即唤醒** | 已在运行时再双击，不再弹“已运行”提示，而是**直接把已有窗口显示出来** |
| **托盘图标** | 右键菜单 → 打开状态窗口（是菜单默认项，左键单击也触发） |

登录自启时（`--start-minimized`）静默驻留托盘不弹窗；用户双击快捷方式时才会显示窗口。
重复唤醒用 `%LOCALAPPDATA%\RecycleBinGuard\wake.request` 这个请求文件实现：
不需要 IPC、不需要权限，进程被强杀后最多残留一个无用文件，会被自动消费掉。

### 托盘右键菜单

| 菜单项 | 行为 |
| --- | --- |
| 打开状态窗口 | 默认项（左键单击也触发） |
| 立即检查 | 派发到后台线程，UI 不冻结 |
| 启用 / 关闭自动清理 | 首次启用前弹不可逆删除风险说明并需明确确认 |
| 试运行模式 | 勾选项，对应 `dry_run` |
| 打开热键（Ctrl + Alt + R） | 勾选项，对应 `hotkey_enabled` |
| 安装开始菜单 / 桌面快捷方式 | 一键创建启动入口，创建结果弹窗反馈 |
| 查看最近清理日志 | 弹出日志窗口，读取轮转日志尾部 |
| 查看清理历史 | 弹出窗口，列出 `clean_history.json` 中的历次清理（时间 / 结果 / 触发方式 / 数量 / 错误），程序重启后仍可读 |
| 开机启动设置 | 勾选项，对应 `start_on_login`；并自愈残留/缺失的启动项 |
| 退出程序 | 停止监控线程与后台执行器，不残留任务 |

### 状态窗口

显示项目总数、阈值、最近检查 / 下次检查、最近清理结果、开机启动、最近错误、配置路径，
以及“立即检查 / 暂停·恢复 / 查看日志 / 隐藏窗口 / 退出”。

窗口顶部一行用标签明确区分：**正在监控 / 已暂停 · 试运行 · 自动清理已启用 / 已关闭 ·
统计不完整 · 清理失败**（`AppController.state_labels()`，可单测）。

### 线程模型

- `tkinter` 只能主线程使用 → `mainloop` 在主线程；
- `pystray.Icon.run` 在守护线程；
- 托盘菜单回调运行在 pystray 线程，**不直接操作控件**，而是把闭包投入
  `queue.Queue`，由主线程每 500ms 取出执行（tkinter 唯一线程安全的做法）；
- 耗时操作（检查 / 复核 / 清空）由 `AppController` 的**单线程执行器**在后台运行；
- 清理确认弹窗走同一条通道：监控线程 → `AppController.request_clean_confirmation()`
  → UI 队列 → 主线程创建弹窗；用户决定再回到后台线程执行复核与清理，
  **弹窗等待期间 UI 不阻塞**（弹窗不使用 `grab_set()` / `wait_window()`）。

### 单实例

使用操作系统级文件锁（Windows `msvcrt.locking`），不是“锁文件存在与否”判断：

- 进程无论正常退出还是被强杀，操作系统都会释放锁 → **异常终止可自恢复**；
- 重复启动时弹出提示并立即退出（退出码 3），不会创建第二个监控实例。

### 配置持久化

所有用户配置存放在 `%LOCALAPPDATA%\RecycleBinGuard\config.json`，原子写入（先写 `.tmp` 再 `replace`）。
测试可用环境变量 `RECYCLEBINGUARD_HOME` 覆盖目录，避免污染真实用户数据。

## T4：开机启动与 EXE 打包

### 开机启动

实现方式：写当前用户的
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 注册表值（`recyclebingguard/autostart.py`）。

- **仅当前用户**，不需要管理员权限，不需要 COM / pywin32；
- 卸载 = 删除该值，干净可查；用户也可在“任务管理器 → 启动”中自行禁用；
- 使用 `HKLM`、任务计划程序、自动提权——**均不采用**；
- 命令形式：
  - 打包后：`"<安装路径>\RecycleBinGuard.exe"`
  - 开发环境：`"<pythonw.exe>" -m recyclebingguard`

### 构建

```powershell
python build.py                # 测试 → 生成图标 → PyInstaller → 冒烟验证
python build.py --skip-tests
```

- 采用 **目录式（onedir）** 构建：`dist/RecycleBinGuard/RecycleBinGuard.exe`；
  单文件模式每次启动都要解压临时目录，启动慢、误报多、难排查，稳定后再考虑；
- `console=False`：GUI 程序不弹控制台；`sys.stdout/stderr` 为 `None` 时入口自动兜底；
- 入口是 `packaging/launcher.py`（绝对导入），避免 PyInstaller 把 `__main__.py`
  当顶层脚本导致相对导入失败；
- 构建产物约 24 MB（含 Python 运行时、tkinter、Pillow、pystray）。

### 命令行（诊断 / 运维）

| 命令 | 说明 |
| --- | --- |
| `--version` | 打印版本 |
| `--init-config` | 写入默认（安全）配置 |
| `--check-once` | 只做一次**只读**统计后退出（不清空任何东西） |
| `--report PATH` | 把命令行输出同时追加写入文件（GUI 版无控制台时用） |
| `--install-shortcuts` | 创建开始菜单 / 桌面快捷方式（当前用户范围） |
| `--remove-shortcuts` | 删除本程序创建的快捷方式 |
| `--start-minimized` | 只驻留托盘不弹窗（开机自启自动带上） |
| `--autostart on\|off\|status` | 设置 / 查询开机启动项（会同步写 `start_on_login`，避免与自愈逻辑冲突） |
| `--uninstall` | 移除开机启动项，**默认保留**配置与日志 |
| `--uninstall --purge` | 同时删除配置与日志 |
| `--console-log` | 同时把日志输出到标准错误 |

打包成 `console=False` 的 GUI 程序后没有控制台，`--check-once` 的输出会丢失。
用 `--report` 落盘即可（GUI 版排查的主要手段）：

```powershell
RecycleBinGuard.exe --check-once --report "$env:USERPROFILE\Desktop\rbg-check.txt"
```

### 崩溃兜底

入口 `entry()` 捕获所有未处理异常，写入 `%LOCALAPPDATA%\RecycleBinGuard\crash.log`，
并返回非零退出码，绝不静默退出。

## 阶段状态

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 骨架 | 目录、配置、日志、接口定义、测试框架 | ✅ 已完成 |
| T1 | 回收站统计与清空核心 | ✅ 已完成 |
| T2 | 阈值监控与安全状态机 | ✅ 已完成 |
| T3 | 系统托盘与通知 | ✅ 已完成 |
| T4 | 开机启动与 EXE 打包 | ✅ 已完成 |
| T5 | 最终验收清单 | 🔍 部分项需人工在专用环境执行，见 [ACCEPTANCE.md](./ACCEPTANCE.md) |

> 自动化测试覆盖了 T5 的 10/13 项；剩下 3 项（真实清空、长期运行、干净环境安装/卸载）
> 必须人工执行，步骤见 **[ACCEPTANCE.md](./ACCEPTANCE.md)**。
