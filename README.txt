================================================================
磁盘文件监控增量同步程序  使用说明
================================================================

【目录结构】
file_sync_monitor\
├── python\                 ← 便携版 Python 3.13 运行时（不依赖系统 Python）
│   └── Lib\site-packages\ ← 已内置 watchdog 依赖
├── file_sync_monitor.py    ← 主程序
├── config.ini              ← 配置文件（监控路径、同步方式都在这里改）
├── requirements.txt        ← 依赖清单（已预装，一般不用管）
├── start.bat               ← 双击启动
├── setup_env.bat           ← 兜底重装依赖（一般不用跑）
└── sync.log                ← 运行后自动生成的日志

【快速开始】
1. 双击 start.bat 即可启动。
2. 程序启动时会先全量扫一次，把源目录里已有的差异文件补到目标端，
   然后进入持续监控。
3. 之后在源目录里新建/修改/删除的文件，会自动同步到目标端
   （默认源端删了目标端也删，可在 config.ini 改）。
4. 关闭：在控制台按 Ctrl+C，或直接关黑窗口。

【三种同步方式（config.ini 每个 pair 的 type 参数）】
- type = local（默认）：目标端是本地磁盘 / 映射盘符路径。
    target = D:\synchronize_backup
- type = smb：目标端是 Windows 共享文件夹（UNC 路径）。
    target = \\192.168.1.20\backup
    username / password：共享需要账号密码时填写，留空则用当前
    Windows 登录身份；填了用户名会先用 net use 建立凭据会话。
    （把共享目录映射成盘符后按 local 配置也可以，效果相同）
- type = ftp：目标端是 FTP 服务器（使用 Python 标准库，无需额外依赖）。
    target = ftp://192.168.1.30:21/backup
    username / password：留空按匿名登录。
    passive：被动模式，一般保持 true；服务器在 NAT/路由器后面必须为 true，
    且服务器端要放行被动端口段（否则"能登录、传不了数据"）。
    tls：true 时使用 FTPS（显式 TLS），服务器支持才开。
  说明：FTP 模式增量判断用 SIZE(大小)+MDTM(修改时间)；上传先传临时名
  再改名，目标端不会出现半成品文件；断线自动重连。

【配置文件 config.ini】
- source / target：要同步的源目录和目标端，支持多组
  （复制 [pair_1] 段改 pair_2，配 type 选择方式）。
- debounce_seconds：防抖秒数，大文件连续写入时别立刻同步，等这么多秒再拷。
- initial_full_scan：启动时是否先全量补一次差异。
- delete_on_source_delete：源端删文件时是否同步删目标端（false 则只增/改不删）。
- log_level / log_file：日志级别和日志文件。
改完 config.ini 重启程序生效。

【没有 FTP/共享环境怎么测试】
- FTP：临时起一个测试服务器（任选其一）：
    pip install pyftpdlib
    python -m pyftpdlib -p 2121 -u test -P 123456 --write -d D:\ftp_test
  然后 pair 配 target = ftp://127.0.0.1:2121/backup。
  正式使用建议装 FileZilla Server（免费）。
- 共享文件夹：本机就能自建一个（管理员运行）：
    net share smb_test=D:\smb_test /grant:everyone,FULL
  然后 pair 配 target = \\localhost\smb_test，走的是完整 SMB 网络栈。

【迁移到新服务器】
整个 file_sync_monitor 文件夹直接拷贝过去即可，无需安装 Python、无需 pip install。
前提：新服务器是 64 位 Windows。
- 若源/目标路径不同，改 config.ini 即可。
- 若依赖丢失（极少见），在新服务器联网状态下双击 setup_env.bat 可自动重装依赖。

【验证记录】
- 本地模式（local）：端到端测试通过（全量扫描/新建/修改/删除均自动同步）。
- FTP 模式（ftp）：用 pyftpdlib 起本机测试服务器做过端到端测试
  （全量上传/新建/修改/删除均自动同步）。
- 共享文件夹模式（smb）：与 local 复用同一套文件操作代码，UNC 路径
  行为一致；在无真实共享环境下，建议按上文方法本机自建共享验证。
