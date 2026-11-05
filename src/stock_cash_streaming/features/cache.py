"""研究因子缓存（目录化、按因子懒加载、可中断续跑）。

全 A 日频因子不写入 SQLite（约 3–4 亿行），而是计算一次后按因子落盘：

    data/research_cache/<key>_<数据版本戳>/
        panel.pkl                 # 统一面板（行情/可交易性/行业映射）
        factors/<因子名>.pkl      # float32 宽表，index=trade_date, columns=symbol
        manifest.json             # 创建时间、因子清单、数据版本戳

设计要点：
- 数据版本戳（财务最新公告日 + 财务行数 + 行情最新交易日）变化即新建目录，
  旧目录只读保留，Web 端可降级使用，不会因为当天数据刚更新就整页 500；
- 写盘按因子逐个进行，进程中断后重跑只补缺失的因子；
- 每批计算前做内存检查（common.mem），余量不足时主动中止并保留已落盘结果；
- Web 请求默认只读缓存（allow_build=False），重计算只允许在 CLI 中执行，
  避免把 uvicorn 推到几十 GB 被内核 OOM kill。
"""

from __future__ import annotations

import gc
import hashlib
import json
import pickle
import shutil
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..common.logging import logger
from ..common.mem import ensure_memory
from ..config.settings import get_settings
from ..storage.repository import Repository
from .fundamental_v5 import compute_v5
from .panel import Panel, _symbols_for, load_panel
from .technical_v4 import compute_all_panel_factors

CACHE_VERSION = "research-v2"
KEEP_DIRS = 2              # 同一股票池/区间最多保留几个数据版本目录
STORED_CHUNK = 600         # 读已存因子时每批股票数


class CacheNotReady(RuntimeError):
    """研究缓存缺失或过期，需要先运行 `cli research-cache`（Web 端不在线重算）。"""


# ---------- 路径与版本戳 ----------

def _cache_root() -> Path:
    root = Path(get_settings().db_path).resolve().parent / "research_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _base_key(universe: str | None, start: str | None, end: str | None,
              symbols: list[str]) -> str:
    h = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:10]
    return f"{CACHE_VERSION}_{universe or 'ALL'}_{start or 'na'}_{end or 'na'}_{h}"


def _data_stamp(repo: Repository) -> str:
    """数据版本戳：财务最新公告日 + 行数 + 行情最新交易日。

    财务/行情回填会改变因子值，必须让旧缓存失效，否则同一策略在不同时间
    会给出不同（且不可复现）的结果。
    """
    fin = repo.query(
        "SELECT IFNULL(MAX(pub_date),'') AS d, COUNT(*) AS n FROM fact_financial")[0]
    q = repo.query("SELECT IFNULL(MAX(trade_date),'') AS d FROM fact_quote")[0]
    raw = f"{fin['d']}|{fin['n']}|{q['d']}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _dirs_for(base: str) -> list[Path]:
    """同一股票池/区间的历史缓存目录，按修改时间从新到旧。"""
    root = _cache_root()
    items = [p for p in root.glob(f"{base}_*") if p.is_dir()]
    return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)


def _manifest(path: Path) -> dict | None:
    f = path / "manifest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_manifest(path: Path, stamp: str) -> dict:
    fdir = path / "factors"
    names = sorted(p.stem for p in fdir.glob("*.pkl"))
    info = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stamp": stamp,
        "factors": names,
        "n_factors": len(names),
    }
    (path / "manifest.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info


def _read_factor(path: Path, name: str) -> pd.DataFrame | None:
    f = path / "factors" / f"{name}.pkl"
    if not f.exists():
        return None
    try:
        return pd.read_pickle(f)
    except (OSError, ValueError, pickle.UnpicklingError) as e:
        logger.warning(f"因子缓存损坏，忽略 {f.name}: {e}")
        return None


def _write_factor(path: Path, name: str, df: pd.DataFrame) -> None:
    fdir = path / "factors"
    fdir.mkdir(parents=True, exist_ok=True)
    tmp = fdir / f".{name}.tmp"
    df.astype("float32").to_pickle(tmp, protocol=4)
    tmp.replace(fdir / f"{name}.pkl")


def _panel_cached(repo: Repository, universe: str | None, start: str | None,
                  end: str | None, path: Path) -> Panel:
    f = path / "panel.pkl"
    if f.exists():
        try:
            with f.open("rb") as fh:
                return pickle.load(fh)
        except (OSError, ValueError, pickle.UnpicklingError) as e:
            logger.warning(f"面板缓存损坏，重新加载：{e}")
    panel = load_panel(repo, universe=universe, start=start, end=end)
    if not panel.close.empty:
        path.mkdir(parents=True, exist_ok=True)
        try:
            with f.open("wb") as fh:
                pickle.dump(panel, fh, protocol=4)
        except OSError as e:
            logger.warning(f"面板缓存写入失败：{e}")
    return panel


def _prune(base: str, keep: Path) -> None:
    """只保留最近 KEEP_DIRS 个数据版本目录，避免 3 GB 级缓存无限堆积。"""
    for i, d in enumerate(_dirs_for(base)):
        if i < KEEP_DIRS or d == keep:
            continue
        try:
            shutil.rmtree(d)
        except OSError as e:
            logger.warning(f"清理旧缓存失败 {d.name}: {e}")


# ---------- 读取已存因子（v2 资金流 / v3 基本面） ----------

def load_stored_factors(repo: Repository, names: list[str], versions: list[str],
                        symbols: list[str], start: str | None, end: str | None,
                        chunk: int = STORED_CHUNK) -> dict[str, pd.DataFrame]:
    """从 fact_factor 读取因子并分批透视成宽表。

    按「因子 × 股票分段」读取：单段最多 chunk 只 × 全区间，透视后立即丢弃长表，
    峰值只与单段规模相关（此前一次性拼接 1.37 亿行长表导致 40 GB 峰值）。
    """
    if not names or not symbols:
        return {}
    ph_v = ",".join("?" for _ in versions)
    out: dict[str, pd.DataFrame] = {}
    for name in names:
        ensure_memory(1.5, f"读取已存因子 {name}")
        wides: list[pd.DataFrame] = []
        for i in range(0, len(symbols), chunk):
            part = symbols[i:i + chunk]
            ph_s = ",".join("?" for _ in part)
            sql = (f"SELECT symbol, trade_date, value FROM fact_factor "
                   f"WHERE factor=? AND version IN ({ph_v}) AND symbol IN ({ph_s})")
            params: list = [name, *versions, *part]
            if start:
                sql += " AND trade_date >= ?"
                params.append(start)
            if end:
                sql += " AND trade_date <= ?"
                params.append(end)
            df = pd.read_sql(sql, repo.conn, params=params)
            if df.empty:
                continue
            w = df.pivot_table(index="trade_date", columns="symbol", values="value",
                               aggfunc="last")
            wides.append(w.astype("float32"))
            del df
        if not wides:
            continue
        frame = pd.concat(wides, axis=1)
        out[name] = frame.reindex(index=frame.index.sort_values(), columns=symbols)
        del wides, frame
    return out


# ---------- 构建 ----------

def build_factor_set(repo: Repository, panel: Panel, with_v5: bool = True,
                     stored_only: bool = False) -> dict[str, pd.DataFrame]:
    """在内存里构建完整因子集（不复用磁盘缓存），供 CLI/测试使用。"""
    from .registry import by_version

    factors: dict[str, pd.DataFrame] = {}
    if not stored_only:
        ensure_memory(4.0, "计算技术因子")
        factors.update(compute_all_panel_factors(panel))
    stored_names = [s.name for s in by_version("v2") + by_version("v3")]
    stored_names = [n for n in stored_names if n not in factors]
    if stored_names:
        factors.update(load_stored_factors(
            repo, stored_names, ["v2", "v3"],
            list(panel.close.columns), str(panel.close.index[0]), str(panel.close.index[-1]),
        ))
    if with_v5:
        ensure_memory(3.0, "计算基本面因子")
        factors.update(compute_v5(repo, panel))
    return factors


def _wanted(names: list[str] | None, path: Path) -> list[str] | None:
    """需要读取/构建的因子清单；None 表示缓存目录里的全部因子。"""
    if names:
        return list(dict.fromkeys(names))
    m = _manifest(path)
    return list(m.get("factors") or []) if m else None


def _load_parts(path: Path, names: list[str]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    got: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for n in names:
        ensure_memory(None, f"读取因子 {n}")
        df = _read_factor(path, n)
        if df is None:
            missing.append(n)
        else:
            got[n] = df
    return got, missing


def _build_into(repo: Repository, panel: Panel, path: Path, stamp: str,
                names: list[str] | None, with_v5: bool) -> dict[str, pd.DataFrame]:
    """按批次计算并逐个因子落盘；已存在的因子跳过（中断续跑）。"""
    from .registry import by_version

    fdir = path / "factors"
    fdir.mkdir(parents=True, exist_ok=True)
    done = {p.stem for p in fdir.glob("*.pkl")}
    want = set(names) if names else None

    def emit(batch: dict[str, pd.DataFrame]) -> int:
        n = 0
        for name, df in batch.items():
            if want is not None and name not in want:
                continue
            if name in done or df is None or df.empty:
                continue
            _write_factor(path, name, df)
            done.add(name)
            n += 1
        return n

    if want is None or bool(want - done):
        ensure_memory(4.5, "计算技术因子（v2/v4）")
        batch = compute_all_panel_factors(panel)
        logger.info(f"技术因子：写入 {emit(batch)} 个（{len(batch)} 个已算）")
        del batch
        gc.collect()

    stored_names = [s.name for s in by_version("v2") + by_version("v3")]
    todo = [n for n in stored_names if n not in done and (want is None or n in want)]
    for i, name in enumerate(todo, 1):
        ensure_memory(1.5, f"读取已存因子 {name}")
        got = load_stored_factors(
            repo, [name], ["v2", "v3"], list(panel.close.columns),
            str(panel.close.index[0]), str(panel.close.index[-1]))
        if name in got:
            _write_factor(path, name, got[name])
            done.add(name)
        del got
        if i % 5 == 0:
            gc.collect()
            logger.info(f"已存因子进度 {i}/{len(todo)}（当前 RSS 见内存日志）")

    if with_v5:
        ensure_memory(3.5, "计算基本面因子（v5）")
        batch = compute_v5(repo, panel)
        logger.info(f"基本面因子：写入 {emit(batch)} 个（{len(batch)} 个已算）")
        del batch
        gc.collect()

    _write_manifest(path, stamp)
    logger.info(f"研究缓存已就绪：{path.name}，因子 {len(done)} 个")
    got, missing = _load_parts(path, names or sorted(done))
    if missing:
        logger.warning(f"以下因子未生成，将在下次构建时重试：{', '.join(missing[:8])}")
    return got


# ---------- 对外入口 ----------

def load_cached_factors(repo: Repository, universe: str | None,
                        start: str | None, end: str | None,
                        names: list[str] | None = None, allow_build: bool = True,
                        force: bool = False, with_v5: bool = True
                        ) -> tuple[Panel, dict[str, pd.DataFrame], dict]:
    """加载面板与因子。

    names      : 只加载/构建这些因子（None 表示全部）
    allow_build: 允许在本次调用里重算（Web 只读接口应传 False）
    force      : 忽略缓存，重新计算并覆盖当前数据版本目录
    """
    syms = _symbols_for(repo, universe)
    base = _base_key(universe, start, end, syms)
    stamp = _data_stamp(repo)
    cur = _cache_root() / f"{base}_{stamp}"
    if force and cur.exists():
        shutil.rmtree(cur, ignore_errors=True)

    # 1) 当前数据版本缓存命中
    m = _manifest(cur)
    if m:
        wanted = _wanted(names, cur)
        if wanted is not None:
            got, missing = _load_parts(cur, wanted)
            if not missing:
                panel = _panel_cached(repo, universe, start, end, cur)
                return panel, got, {"cache": "hit", "dir": cur.name,
                                    "created_at": m.get("created_at")}

    # 2) 降级：读取最近一次可用的旧目录（数据更新当天不至于整页不可用）
    for d in _dirs_for(base):
        if d == cur:
            continue
        md = _manifest(d)
        if not md:
            continue
        wanted = _wanted(names, d)
        if wanted is None:
            continue
        got, missing = _load_parts(d, wanted)
        if not missing:
            panel = _panel_cached(repo, universe, start, end, d)
            return panel, got, {"cache": "stale", "dir": d.name,
                                "created_at": md.get("created_at"),
                                "stamp": md.get("stamp")}

    # 3) 未命中：只读调用直接报错，避免 Web 进程重算把内存打爆
    if not allow_build:
        raise CacheNotReady(
            f"研究因子缓存未就绪（{universe or 'ALL'}），"
            f"请先运行 cli research-cache --universe {universe or 'A0'}")

    panel = _panel_cached(repo, universe, start, end, cur)
    if panel.close.empty:
        return panel, {}, {"cache": "empty"}
    factors = _build_into(repo, panel, cur, stamp, names, with_v5)
    _prune(base, cur)
    return panel, factors, {"cache": "built", "dir": cur.name,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def available_factors(repo: Repository, universe: str | None,
                      start: str | None = None, end: str | None = None) -> list[str]:
    """当前缓存里可用的因子名（读 manifest，不加载数据），供 Web 端裁剪请求。"""
    base = _base_key(universe, start, end, _symbols_for(repo, universe))
    for d in _dirs_for(base):
        m = _manifest(d)
        if m:
            return list(m.get("factors") or [])
    return []


def cache_status() -> list[dict]:
    """列出全部研究缓存目录（供 CLI/运维查看）。"""
    root = _cache_root()
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        m = _manifest(p) or {}
        fdir = p / "factors"
        n_files = len(list(fdir.glob("*.pkl"))) if fdir.exists() else 0
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        out.append({
            "dir": p.name, "created_at": m.get("created_at"), "stamp": m.get("stamp"),
            "factors": n_files, "size_gb": round(size / 1073741824, 2),
            "age_days": round((time.time() - p.stat().st_mtime) / 86400, 1),
            "ready": bool(m),
        })
    return out
