"""159207 高股息ETF分析 API：读取最新结果、后台运行分析、查看报告。"""

from __future__ import annotations

import html
import json
import math
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "etf_159207_rebalance.py"
CACHE = ROOT / "data" / "etf159207_cache"
DOCS = ROOT / "docs"
REPORT = DOCS / "etf_159207_rebalance_analysis.md"

router = APIRouter(prefix="/api/etf159207", tags=["etf159207"])

_LOCK = threading.Lock()
_JOB = {
    "running": False,
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": deque(maxlen=400),
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(obj):
    """把 NaN/Inf/numpy 标量转成 JSON 安全值。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if hasattr(obj, "item"):
        return _clean(obj.item())
    return obj


def _read_csv(name: str) -> list[dict]:
    path = DOCS / name
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"symbol": str})
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _read_metrics() -> dict | None:
    path = CACHE / "metrics.json"
    if not path.exists():
        return None
    try:
        return _clean(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None


def _read_last_run() -> dict | None:
    path = CACHE / "last_run.json"
    if not path.exists():
        return None
    try:
        return _clean(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None


def _status() -> dict:
    with _LOCK:
        state = {
            "running": _JOB["running"],
            "run_id": _JOB["run_id"],
            "started_at": _JOB["started_at"],
            "finished_at": _JOB["finished_at"],
            "returncode": _JOB["returncode"],
            "log": list(_JOB["log"])[-120:],
        }
    last = _read_last_run()
    metrics_path = CACHE / "metrics.json"
    state["last_run"] = last
    state["result_at"] = (
        datetime.fromtimestamp(metrics_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if metrics_path.exists()
        else None
    )
    return state


def _run_job() -> None:
    cmd = [sys.executable, "-u", str(SCRIPT), "run"]
    with _LOCK:
        _JOB["log"].append("$ " + " ".join(cmd))
    rc = -1
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                with _LOCK:
                    _JOB["log"].append(line)
        rc = proc.wait()
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            _JOB["log"].append(f"[ERROR] {type(e).__name__}: {e}")
    finished = _now()
    with _LOCK:
        _JOB["running"] = False
        _JOB["finished_at"] = finished
        _JOB["returncode"] = rc
        run_id = _JOB["run_id"]
    payload = {
        "run_id": run_id,
        "started_at": _JOB["started_at"],
        "finished_at": finished,
        "returncode": rc,
        "status": "ok" if rc == 0 else "failed",
    }
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / "last_run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


@router.post("/run")
def run() -> dict:
    """后台按最新行情运行分析（约 1~3 分钟，取决于行情接口速度）。"""
    with _LOCK:
        if _JOB["running"]:
            raise HTTPException(status_code=409, detail="分析正在运行中，请稍候")
        _JOB.update(
            running=True,
            run_id=datetime.now().strftime("%Y%m%d-%H%M%S"),
            started_at=_now(),
            finished_at=None,
            returncode=None,
        )
        _JOB["log"].clear()
        _JOB["log"].append("任务已启动：刷新最新行情并重算 …")
    threading.Thread(target=_run_job, daemon=True).start()
    return {"status": "started", "run_id": _JOB["run_id"]}


@router.get("/status")
def status() -> dict:
    return _status()


@router.get("/latest")
def latest() -> dict:
    """最近一次成功运行的结构化结果 + Markdown 报告。"""
    metrics = _read_metrics()
    if not metrics:
        return {"ready": False, "status": _status(), "message": "尚无运行结果，请点击 Run"}
    try:
        pred = _read_csv("etf_159207_q4_prediction.csv")
        holdings = _read_csv("etf_159207_holdings_top10.csv")
        weights = _read_csv("etf_159207_index_weights.csv")
        report = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
    except Exception as e:  # noqa: BLE001
        return {
            "ready": False,
            "status": _status(),
            "message": f"结果文件正在更新或读取失败：{e}",
        }
    return {
        "ready": True,
        "generated_at": metrics.get("generated_at"),
        "data_as_of": metrics.get("data_as_of"),
        "return_asof": metrics.get("return_asof"),
        "weight_asof": metrics.get("weight_asof"),
        "sim_start": metrics.get("sim_start"),
        "sim_end": metrics.get("sim_end"),
        "fund": metrics.get("fund_info", {}),
        "summary": {
            **metrics.get("prediction", {}),
            "dividend_check": metrics.get("dividend_check", {}),
            "weight_model": metrics.get("weight_model", {}),
            "etf_vs_index": metrics.get("etf_vs_index_0930", {}),
        },
        "prediction": pred,
        "holdings": holdings,
        "weights": weights,
        "turnover": metrics.get("turnover", []),
        "validation": {
            "dividend_check": metrics.get("dividend_check", {}),
            "weight_model": metrics.get("weight_model", {}),
            "etf_vs_index": metrics.get("etf_vs_index_0930", {}),
            "drift": metrics.get("drift", []),
        },
        "since_last_report": metrics.get("since_last_report", {}),
        "boundary_threshold": metrics.get("boundary_threshold", []),
        "report_markdown": report,
        "report_url": "/api/etf159207/report",
        "status": _status(),
    }


@router.get("/report", response_class=HTMLResponse)
def report() -> str:
    if not REPORT.exists():
        raise HTTPException(status_code=404, detail="报告尚未生成")
    text = REPORT.read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>159207 研究报告</title>
<style>
body{{margin:0;padding:24px;background:#0b1220;color:#e8eefb;
     font:13px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}}
pre{{white-space:pre-wrap;word-break:break-word;max-width:1100px;margin:0 auto;
     background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);
     border-radius:12px;padding:20px}}
a{{color:#67e8f9}}
</style></head><body>
<div style="max-width:1100px;margin:0 auto 12px"><a href="/api/etf159207/report">查看原文</a></div>
<pre>{html.escape(text)}</pre>
</body></html>"""
