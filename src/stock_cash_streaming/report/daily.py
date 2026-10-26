"""AI 日报：优先调用 DeepSeek（OpenAI 兼容），失败降级为规则模板。

消息面参考来自 DeepSeek Search：只作定性材料展示（含来源与链接），
不写入数据库，不参与因子、信号、组合与回测。
"""

from datetime import datetime, timedelta

from ..config.settings import get_settings
from ..datasource.web_search import search
from ..storage.repository import open_repository


def _context(repo) -> dict:
    flow_date = repo.query("SELECT MAX(trade_date) AS d FROM fact_stock_flow")[0]["d"]
    sector_flow = [dict(r) for r in repo.query(
        "SELECT sector_code, main_net FROM fact_sector_flow WHERE trade_date=? ORDER BY main_net DESC", (flow_date,))]
    signals = [dict(r) for r in repo.query(
        """SELECT g.symbol, s.name, g.score, g.grade, g.reasons, m.sector_code
           FROM fact_signal g JOIN dim_stock s ON s.symbol=g.symbol
           JOIN dim_sector_member m ON m.symbol=g.symbol
           WHERE g.trade_date=(SELECT MAX(trade_date) FROM fact_signal) ORDER BY g.score DESC""")]
    return {"date": flow_date, "sector_flow": sector_flow, "signals": signals}


def _news_context() -> list[dict]:
    """按目标板块检索近期消息，按 URL 去重。检索失败返回空列表。"""
    cfg = get_settings()
    if not cfg.search_enabled:
        return []
    since = (datetime.now() - timedelta(days=cfg.search_days)).strftime("%Y-%m-%d")
    seen: set[str] = set()
    items: list[dict] = []
    for sector in cfg.sectors:
        rows = search(f"{sector} 板块 政策 行业 动态 lang:zh after:{since}", count=cfg.search_count)
        for r in rows:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            items.append({**r, "sector": sector})
    return items


def _news_block(news: list[dict]) -> str:
    """把检索结果渲染成带来源链接的纯文本区块。"""
    if not news:
        return ""
    lines = ["", "", "### 消息面参考（网页检索，不构成信号）"]
    for n in news:
        lines.append(f"- {n['date'] or '日期未知'}｜{n['site'] or '来源未知'}：{n['title']}")
        if n.get("summary"):
            lines.append(f"  {n['summary'][:120]}")
        lines.append(f"  {n['url']}")
    return "\n".join(lines)


def _fallback(ctx: dict, news: list[dict] | None = None) -> str:
    lines = [f"## {ctx['date']} 资金流日报（规则生成）", "", "### 板块资金流"]
    for s in ctx["sector_flow"]:
        v = (s["main_net"] or 0) / 1e8
        lines.append(f"- {s['sector_code']}：主力净流入 {v:+.2f} 亿元")
    lines += ["", "### 信号清单"]
    for s in ctx["signals"]:
        lines.append(f"- [{s.get('sector_code','')}] {s['name']}({s['symbol']})：{s['score']:.1f} 分 / {s['grade']}")
    lines += ["", "> 本内容为基于历史数据的量化研究结果，不构成投资建议。"]
    return "\n".join(lines) + _news_block(news or [])


def generate_daily_report() -> dict:
    repo = open_repository()
    try:
        ctx = _context(repo)
    finally:
        repo.close()

    cfg = get_settings()
    news = _news_context()
    if not cfg.openai_api_key:
        return {"source": "template", "content": _fallback(ctx, news), "news": news}

    news_text = "\n".join(
        f"- {n['date']}｜{n['site']}｜{n['title']}｜{n['summary'][:200]}｜{n['url']}"
        for n in news
    ) or "（本次未检索到相关消息）"

    prompt = (
        "你是A股量化研究助手。请根据以下资金流与信号数据，写一份简洁的中文日报（300字以内），"
        "结构为：1) 板块资金流概览；2) 值得关注的个股及原因；3) 风险提示。"
        "注意：本系统信号采用防御型评分（低波动、低换手、低彩票特征、非流动性溢价），"
        "分数高表示「风险特征更防御」，不代表看涨。"
        "只做客观分析，不要给出买入/卖出建议或目标价。\n"
        "另附网页检索到的近期消息，仅可用于定性描述消息面与政策面，"
        "不得据此推断价格，不得编造材料中不存在的信息。\n\n"
        f"数据：{ctx}\n\n参考消息：\n{news_text}"
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url)
        resp = client.chat.completions.create(
            model=cfg.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        content = resp.choices[0].message.content or ""
        return {"source": cfg.llm_model, "content": content + _news_block(news), "news": news}
    except Exception as e:  # noqa: BLE001
        return {"source": "template(fallback)", "content": _fallback(ctx, news),
                "error": str(e), "news": news}
