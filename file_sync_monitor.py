# -*- coding: utf-8 -*-
"""
磁盘文件监控增量同步程序（目标端支持：本地磁盘 / Windows 共享文件夹 / FTP）
- 通过 config.ini 中每个 [pair_x] 的 type 参数选择同步方式：
    type = local  本地磁盘或映射盘符（默认，可省略）
    type = smb    Windows 共享文件夹（UNC 路径，如 \\192.168.1.20\backup）
    type = ftp    FTP 服务器（如 ftp://192.168.1.30:21/backup）
- 监听配置文件中声明的多个同步对（源 -> 目标）
- 启动时先做一次全量增量同步（只复制缺失/更新的文件）
- 运行时基于 watchdog 监听文件创建/修改/移动/删除，防抖后增量同步
- 源端始终是本机目录；目标端目录结构与源端保持一致
- FTP 使用 Python 标准库 ftplib，无需额外依赖
"""

import calendar
import configparser
import ftplib
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# ----------------------------- 日志 -----------------------------
def setup_logger(log_file: str, level: str) -> logging.Logger:
    logger = logging.getLogger("sync")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    # 控制台
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # 文件
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ----------------------------- 工具 -----------------------------
def rel_posix(source_root: Path, path) -> str:
    """源端绝对路径 -> 相对 source_root 的 POSIX 风格相对路径（根本身返回空串）。"""
    p = Path(path).resolve().relative_to(source_root)
    return "" if str(p) == "." else p.as_posix()


# ----------------------------- 同步后端抽象 -----------------------------
class SyncBackend:
    """
    目标端抽象。所有 rel 参数均为相对目标根目录的 POSIX 风格相对路径（如 a/b/c.txt）。
    子类需实现：ensure_root / file_stat / dir_exists / mkdirs /
               copy_file / remove_file / remove_dir
    """

    def __init__(self, logger: logging.Logger, verify: bool):
        self.logger = logger
        self.verify = verify

    def describe(self, rel: str = "") -> str:
        return rel or "<target>"

    def ensure_root(self):
        raise NotImplementedError

    def file_stat(self, rel: str):
        """返回 (size, mtime)；文件不存在返回 None。"""
        raise NotImplementedError

    def dir_exists(self, rel: str) -> bool:
        raise NotImplementedError

    def mkdirs(self, rel: str):
        raise NotImplementedError

    def copy_file(self, src: Path, rel: str):
        raise NotImplementedError

    def remove_file(self, rel: str):
        raise NotImplementedError

    def remove_dir(self, rel: str):
        """递归删除目录及其内容。"""
        raise NotImplementedError

    def differ(self, src: Path, rel: str) -> bool:
        """增量判断：目标缺失 / 大小不同 / 源端 mtime 更新 -> 需要同步。"""
        st = self.file_stat(rel)
        if st is None:
            return True
        s = src.stat()
        if s.st_size != st[0]:
            return True
        if s.st_mtime - st[1] > 1.0:
            return True
        return False


# ----------------------------- 本地 / SMB 后端 -----------------------------
class LocalBackend(SyncBackend):
    """本地磁盘 / 映射盘符 / UNC 共享路径（smb 类型复用此类，UNC 路径可直接用）。"""

    def __init__(self, root: Path, logger: logging.Logger, verify: bool):
        super().__init__(logger, verify)
        self.root = root

    def _p(self, rel: str) -> Path:
        return self.root / rel

    def describe(self, rel: str = "") -> str:
        return str(self._p(rel)) if rel else str(self.root)

    def ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True)

    def file_stat(self, rel: str):
        try:
            st = self._p(rel).stat()
            return st.st_size, st.st_mtime
        except OSError:
            return None

    def dir_exists(self, rel: str) -> bool:
        return self._p(rel).is_dir()

    def mkdirs(self, rel: str):
        self._p(rel).mkdir(parents=True, exist_ok=True)

    def copy_file(self, src: Path, rel: str):
        dst = self._p(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                # 先用临时名写，写完再改名，避免目标端读到半成品
                tmp = dst.with_suffix(dst.suffix + ".tmp_sync")
                shutil.copy2(src, tmp)
                if self.verify and tmp.stat().st_size != src.stat().st_size:
                    raise IOError("size mismatch during copy")
                os.replace(tmp, dst)
                self.logger.info(f"[复制] {src} -> {dst}")
                return
            except (PermissionError, OSError) as e:
                if attempt == 1:
                    self.logger.warning(f"[失败] {src} 复制失败：{e}")
                else:
                    time.sleep(0.5)

    def remove_file(self, rel: str):
        try:
            self._p(rel).unlink(missing_ok=True)
            self.logger.info(f"[删除] {self.describe(rel)}")
        except OSError as e:
            self.logger.warning(f"[删除失败] {self.describe(rel)}: {e}")

    def remove_dir(self, rel: str):
        try:
            shutil.rmtree(self._p(rel), ignore_errors=True)
            self.logger.info(f"[删除] {self.describe(rel)}")
        except OSError as e:
            self.logger.warning(f"[删除失败] {self.describe(rel)}: {e}")


class SmbBackend(LocalBackend):
    """Windows 共享文件夹：UNC 路径下文件操作与本地一致，凭据见 smb_connect()。"""
    pass


def smb_connect(target: str, username: str, password: str, logger: logging.Logger):
    """提供了用户名时，用 net use 预先建立到共享的凭据会话（持久性为否）。"""
    if not username:
        return
    m = re.match(r"^\\\\([^\\/]+)[\\/](.+?)(?:\\|$)", target.replace("/", "\\"))
    if not m:
        logger.warning(f"[SMB] 无法从 target 解析 UNC：{target}，跳过凭据会话")
        return
    unc = f"\\\\{m.group(1)}\\{m.group(2)}"
    cmd = ["net", "use", unc]
    if password:
        cmd.append(password)
    cmd.append("/user:" + username)
    cmd.append("/persistent:no")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            logger.warning(
                "[SMB] net use 建立会话失败（若此前已连接可忽略）："
                f"{(r.stderr or r.stdout or '').strip()}"
            )
        else:
            logger.info(f"[SMB] 已建立共享会话：{unc}（用户 {username}）")
    except Exception as e:
        logger.warning(f"[SMB] net use 执行失败：{e}")


# ----------------------------- FTP 后端 -----------------------------
class FtpBackend(SyncBackend):
    """
    FTP 目标端。连接后统一 cd 到 base 目录，所有 rel 路径均相对该目录。
    增量判断用 SIZE（大小）+ MDTM（修改时间）；上传用 临时名 STOR + RNTO 改名。
    连接断开自动重连（重试一次）。
    """

    def __init__(self, host, port, user, password, base_dir,
                 logger: logging.Logger, verify: bool,
                 passive: bool = True, tls: bool = False):
        super().__init__(logger, verify)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.base = base_dir.strip("/")
        self.passive = passive
        self.tls = tls
        self._conn = None
        self._abs_base = "/"
        self._dirs_ok = set()
        self._lock = threading.RLock()

    # ---- 连接管理 ----
    def _drop(self):
        try:
            if self._conn is not None:
                self._conn.quit()
        except Exception:
            pass
        self._conn = None

    def _connect(self):
        conn = ftplib.FTP_TLS() if self.tls else ftplib.FTP()
        conn.connect(self.host, self.port, timeout=30)
        conn.login(self.user or "anonymous", self.password or "")
        conn.set_pasv(self.passive)
        if self.tls:
            conn.prot_p()
        conn.voidcmd("TYPE I")  # 二进制模式，SIZE 才可靠
        if self.base:
            try:
                conn.cwd(self.base)
            except ftplib.error_perm:
                # base 目录不存在则逐级创建
                acc = ""
                for part in self.base.split("/"):
                    if not part:
                        continue
                    acc = f"{acc}/{part}" if acc else part
                    try:
                        conn.mkd(acc)
                    except ftplib.error_perm:
                        pass  # 已存在
                conn.cwd(self.base)
        self._abs_base = conn.pwd()
        self._conn = conn
        self._dirs_ok = set()
        self.logger.info(f"[FTP] 已连接 {self.host}:{self.port}，目标根目录 {self._abs_base}")

    def _ensure(self):
        if self._conn is not None:
            try:
                self._conn.voidcmd("NOOP")
                return self._conn
            except ftplib.all_errors:
                self._drop()
        self._connect()
        return self._conn

    def _back_to_base(self, conn):
        try:
            conn.cwd(self._abs_base)
        except ftplib.all_errors:
            self._drop()

    def describe(self, rel: str = "") -> str:
        full = posixpath.join(self.base, rel) if rel else self.base
        return f"ftp://{self.host}:{self.port}/{full}"

    def ensure_root(self):
        with self._lock:
            self._ensure()

    # ---- 基本操作 ----
    def file_stat(self, rel: str):
        with self._lock:
            try:
                conn = self._ensure()
            except ftplib.all_errors as e:
                self.logger.warning(f"[FTP] 连接失败：{e}")
                return None
            try:
                size = conn.size(rel)
            except ftplib.error_perm:
                size = None
            except ftplib.all_errors:
                self._drop()
                return None
            mtime = None
            try:
                resp = conn.sendcmd("MDTM " + rel)
                m = re.match(r"^213 (\d{14})", resp)
                if m:
                    mtime = calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
            except ftplib.error_perm:
                pass
            except ftplib.all_errors:
                self._drop()
                return None
            if size is None and mtime is None:
                return None  # 文件不存在
            return (size if size is not None else -1), (mtime if mtime is not None else 0.0)

    def dir_exists(self, rel: str) -> bool:
        with self._lock:
            try:
                conn = self._ensure()
            except ftplib.all_errors:
                return False
            try:
                conn.cwd(rel)
                return True
            except ftplib.error_perm:
                return False
            finally:
                self._back_to_base(conn)

    def mkdirs(self, rel: str):
        with self._lock:
            try:
                conn = self._ensure()
            except ftplib.all_errors as e:
                self.logger.warning(f"[FTP] 连接失败：{e}")
                return
            acc = ""
            for part in rel.split("/"):
                if not part:
                    continue
                acc = f"{acc}/{part}" if acc else part
                if acc in self._dirs_ok:
                    continue
                try:
                    conn.mkd(acc)
                except ftplib.error_perm:
                    # 可能已存在，确认一下
                    try:
                        conn.cwd(acc)
                        conn.cwd(self._abs_base)
                    except ftplib.all_errors:
                        self.logger.warning(f"[FTP] 无法创建目录 {self.describe(acc)}")
                self._dirs_ok.add(acc)

    def copy_file(self, src: Path, rel: str):
        with self._lock:
            for attempt in range(2):
                try:
                    conn = self._ensure()
                    tmp = rel + ".tmp_sync"
                    with open(src, "rb") as f:
                        conn.storbinary("STOR " + tmp, f)
                    if self.verify:
                        rsize = conn.size(tmp)
                        if rsize is not None and rsize != src.stat().st_size:
                            raise IOError("size mismatch after upload")
                    # 先删旧目标再改名（部分服务器不允许 RNTO 直接覆盖）
                    try:
                        conn.delete(rel)
                    except ftplib.error_perm:
                        pass
                    conn.rename(tmp, rel)
                    self.logger.info(f"[复制] {src} -> {self.describe(rel)}")
                    return
                except (ftplib.all_errors, OSError) as e:
                    self._drop()
                    if attempt == 1:
                        self.logger.warning(f"[失败] {src} 上传失败：{e}")
                    else:
                        time.sleep(0.5)

    def remove_file(self, rel: str):
        with self._lock:
            for attempt in range(2):
                try:
                    conn = self._ensure()
                    try:
                        conn.delete(rel)
                    except ftplib.error_perm as e:
                        if str(e).startswith("550"):
                            return  # 目标本就不存在
                        raise
                    self.logger.info(f"[删除] {self.describe(rel)}")
                    return
                except ftplib.all_errors as e:
                    self._drop()
                    if attempt == 1:
                        self.logger.warning(f"[删除失败] {self.describe(rel)}: {e}")
                    else:
                        time.sleep(0.5)

    def remove_dir(self, rel: str):
        with self._lock:
            try:
                self._rmtree(rel)
                self.logger.info(f"[删除] {self.describe(rel)}")
            except ftplib.all_errors as e:
                self.logger.warning(f"[删除失败] {self.describe(rel)}: {e}")

    # ---- FTP 内部辅助 ----
    def _safe_list(self, conn, path):
        """列目录，返回 [(name, is_dir)]；is_dir 为 True/False/None(未知)。"""
        try:
            out = []
            for name, facts in conn.mlsd(path):
                t = facts.get("type", "")
                if t in ("cdir", "pdir"):
                    continue
                out.append((name, (t == "dir") if t else None))
            return out
        except ftplib.error_perm as e:
            if str(e)[:3] in ("500", "502", "504"):
                # 服务器不支持 MLSD，退化为 NLST（类型未知）
                out = []
                for n in conn.nlst(path):
                    b = posixpath.basename(n.rstrip("/"))
                    if b and b not in (".", ".."):
                        out.append((b, None))
                return out
            return []  # 目录不存在或不可访问

    def _rmtree(self, rel):
        conn = self._ensure()
        for name, is_dir in self._safe_list(conn, rel):
            child = f"{rel}/{name}"
            if is_dir is False:
                conn.delete(child)
            elif is_dir is True:
                self._rmtree(child)
            else:  # 类型未知：先按文件删，失败再按目录递归
                try:
                    conn.delete(child)
                except ftplib.error_perm:
                    self._rmtree(child)
        try:
            conn.rmd(rel)
        except ftplib.error_perm as e:
            if not str(e).startswith("550"):
                raise


# ----------------------------- 后端工厂 -----------------------------
def create_backend(pair: dict, general: dict, logger: logging.Logger) -> SyncBackend:
    t = pair["type"]
    verify = general["verify"]
    if t == "local":
        return LocalBackend(Path(pair["target"]), logger, verify)
    if t == "smb":
        smb_connect(pair["target"], pair["username"], pair["password"], logger)
        return SmbBackend(Path(pair["target"]), logger, verify)
    if t == "ftp":
        u = urllib.parse.urlparse(pair["target"])
        if u.scheme != "ftp" or not u.hostname:
            raise ValueError(
                f"ftp 类型 target 必须是 ftp://主机[:端口]/目录 形式：{pair['target']}")
        return FtpBackend(
            u.hostname, u.port or 21,
            pair["username"], pair["password"],
            u.path.strip("/"), logger, verify,
            passive=pair["passive"], tls=pair["tls"],
        )
    raise ValueError(f"未知的同步类型：{t}（支持 local / smb / ftp）")


# ----------------------------- 事件处理器 -----------------------------
class SyncHandler(FileSystemEventHandler):
    def __init__(self, source_root: Path, backend: SyncBackend, cfg, logger):
        self.source_root = source_root.resolve()
        self.backend = backend
        self.cfg = cfg
        self.logger = logger
        # 防抖队列：path -> (next_process_time, event_type)
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._debounce_loop, daemon=True)
        self._worker.start()

    # ---- 事件入队 ----
    def _enqueue(self, src_path: str, kind: str):
        try:
            Path(src_path).resolve().relative_to(self.source_root)
        except (ValueError, FileNotFoundError):
            return
        with self._lock:
            self._pending[src_path] = (
                time.time() + self.cfg["debounce"],
                kind,
            )

    def on_created(self, event):
        self._enqueue(event.src_path, "sync_dir" if event.is_directory else "sync_file")

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path, "sync_file")

    def on_moved(self, event):
        # watchdog 对目录移动也会触发；dest_path 是新位置
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue(dest, "sync_dir" if event.is_directory else "sync_file")
            # 旧位置若配置了删除同步，就清理掉
            if self.cfg["delete_on_del"]:
                with self._lock:
                    self._pending[event.src_path] = (time.time() + 0.2, "delete")

    def on_deleted(self, event):
        if self.cfg["delete_on_del"]:
            with self._lock:
                self._pending[event.src_path] = (time.time() + 0.2, "delete")

    # ---- 防抖消费线程 ----
    def _debounce_loop(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            now = time.time()
            due = []
            with self._lock:
                for p, (t, kind) in list(self._pending.items()):
                    if now >= t:
                        due.append((p, kind))
                        del self._pending[p]
            for p, kind in due:
                try:
                    self._dispatch(p, kind)
                except Exception as e:
                    self.logger.warning(f"[处理异常] {p}: {e}")

    def _dispatch(self, src_path: str, kind: str):
        rel = rel_posix(self.source_root, src_path)
        if not rel:
            return
        if kind == "delete":
            if self.backend.dir_exists(rel):
                self.backend.remove_dir(rel)
            elif self.backend.file_stat(rel) is not None:
                self.backend.remove_file(rel)
            return
        if kind == "sync_dir":
            self.backend.mkdirs(rel)
            return
        # sync_file
        src = Path(src_path)
        if not src.is_file():
            return
        if self.backend.differ(src, rel):
            self.backend.copy_file(src, rel)

    def stop(self):
        self._stop.set()


# ----------------------------- 全量扫描 -----------------------------
def initial_sync(source_root: Path, backend: SyncBackend, logger: logging.Logger):
    logger.info(f"开始全量增量扫描：{source_root} -> {backend.describe()}")
    backend.ensure_root()
    synced = 0
    for dirpath, dirnames, filenames in os.walk(source_root):
        rel_dir = rel_posix(source_root, dirpath)
        if rel_dir:
            backend.mkdirs(rel_dir)
        for name in filenames:
            src = Path(dirpath) / name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                if backend.differ(src, rel):
                    backend.copy_file(src, rel)
                    synced += 1
            except Exception as e:
                logger.warning(f"[失败] {src}：{e}")
    logger.info(f"全量扫描完成，本次新增/更新 {synced} 个文件")


# ----------------------------- 配置 -----------------------------
def load_config(path: str):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    general = {
        "debounce": cp.getfloat("general", "debounce_seconds", fallback=2.0),
        "full_scan": cp.getboolean("general", "initial_full_scan", fallback=True),
        "delete_on_del": cp.getboolean("general", "delete_on_source_delete", fallback=True),
        "log_level": cp.get("general", "log_level", fallback="INFO"),
        "log_file": cp.get("general", "log_file", fallback=""),
        "verify": cp.getboolean("general", "verify_size", fallback=True),
    }
    pairs = []
    for sec in cp.sections():
        if not sec.startswith("pair_"):
            continue
        src = cp.get(sec, "source", fallback="").strip()
        dst = cp.get(sec, "target", fallback="").strip()
        if not (src and dst):
            continue
        pairs.append({
            "type": cp.get(sec, "type", fallback="local").strip().lower(),
            "source": Path(src),
            "target": dst,
            "username": cp.get(sec, "username", fallback="").strip(),
            "password": cp.get(sec, "password", fallback="").strip(),
            "passive": cp.getboolean(sec, "passive", fallback=True),
            "tls": cp.getboolean(sec, "tls", fallback=False),
        })
    return general, pairs


# ----------------------------- 主流程 -----------------------------
def main():
    here = Path(__file__).resolve().parent
    # 支持通过命令行 --config 指定配置文件，默认用同目录 config.ini
    cfg_path = here / "config.ini"
    for i, a in enumerate(sys.argv):
        if a in ("-c", "--config") and i + 1 < len(sys.argv):
            cfg_path = Path(sys.argv[i + 1])
            break
    if not cfg_path.exists():
        print(f"配置文件不存在：{cfg_path}", file=sys.stderr)
        sys.exit(1)

    general, pairs = load_config(str(cfg_path))
    logger = setup_logger(
        str(here / general["log_file"]) if general["log_file"] else "",
        general["log_level"],
    )

    if not pairs:
        logger.error("配置文件中没有有效的 [pair_x] 同步对，退出。")
        sys.exit(1)

    observer = Observer()
    handlers = []

    for pair in pairs:
        src = pair["source"]
        if not src.exists():
            logger.warning(f"源目录不存在，已跳过：{src}（程序仍在运行，目录出现后需重启才会监控）")
            continue
        try:
            backend = create_backend(pair, general, logger)
            backend.ensure_root()
        except Exception as e:
            logger.error(
                f"目标端初始化失败（type={pair['type']}，target={pair['target']}）：{e}"
            )
            continue
        logger.info(f"注册同步对（{pair['type']}）：{src}  ->  {backend.describe()}")
        if general["full_scan"]:
            try:
                initial_sync(src.resolve(), backend, logger)
            except Exception as e:
                logger.warning(f"全量扫描异常：{e}")
        h = SyncHandler(src, backend, general, logger)
        observer.schedule(h, str(src), recursive=True)
        handlers.append(h)

    observer.start()
    logger.info("=" * 50)
    logger.info("监控已启动，按 Ctrl+C 退出。")
    logger.info("=" * 50)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在停止...")
    finally:
        for h in handlers:
            h.stop()
        observer.stop()
        observer.join(timeout=5)
        logger.info("已退出。")


if __name__ == "__main__":
    main()
