"""命令行入口。

用法：
    python -m stock_cash_streaming.cli init  [--start 20200101] [--end 20261014] [--limit 20]
    python -m stock_cash_streaming.cli seed  # 载入 MCP 种子数据
    python -m stock_cash_streaming.cli status
"""

import argparse
from datetime import datetime

from .common.logging import setup_logging, logger
from .datasource.registry import get_source
from .ingest.history import init_history
from .storage.repository import open_repository


def _cmd_init(args) -> None:
    repo = open_repository()
    ds = get_source()
    logger.info(f"数据源: {ds.name} | 区间: {args.start} ~ {args.end} | limit={args.limit}")
    try:
        stats = init_history(ds, repo, args.start, args.end, args.limit)
    finally:
        repo.close()
    print("\n===== 初始化统计 =====")
    print(f"板块数         : {stats['sectors']}")
    print(f"成分股(处理)   : {stats['members']}")
    print(f"行情成功/失败  : {stats['price_ok']} / {stats['price_fail']}")
    print(f"个股资金流快照 : {stats['flow_ok']}")
    print(f"板块资金流     : {stats['sector_flow']}")


def _cmd_seed(_args) -> None:
    from .ingest.mcp_loader import load_seed

    repo = open_repository()
    try:
        stats = load_seed(repo)
    finally:
        repo.close()
    print("\n===== 种子载入统计 =====")
    print(f"成分股 : {stats['members']}")
    print(f"资金流 : {stats['fund_flow_rows']} 行")


def _cmd_factors(args) -> None:
    from .features.engine import RESEARCH_FACTORS, compute_all
    from .features.fundamentals import compute_fundamental_factors

    repo = open_repository()
    try:
        if args.lite:
            stats = compute_all(repo, include_flow=False, keep_factors=RESEARCH_FACTORS)
            print("\n===== 因子计算完成（lite：9 个技术因子）=====")
        else:
            stats = compute_all(repo)
            print("\n===== 因子计算完成 =====")
        for k, v in sorted(stats.items()):
            print(f"  {k:<22} {v} 行")
        if not args.no_fundamentals:
            fs = compute_fundamental_factors(repo)
            if fs:
                print("\n===== 基本面因子（v3）=====")
                for k, v in sorted(fs.items()):
                    print(f"  {k:<22} {v} 行")
    finally:
        repo.close()


def _cmd_signals(args) -> None:
    from .strategy.signal import generate_signals

    repo = open_repository()
    try:
        sigs = generate_signals(repo, args.date)
    finally:
        repo.close()
    if not sigs:
        print("无信号")
        return
    print(f"\n===== 信号（{sigs[0]['trade_date']}）=====")
    for x in sigs:
        rs = "；".join(x["reasons"]) or "-"
        print(f"  [{x['sector']}] {x['symbol']} {x['name']:<5} 分数={x['score']:>6} {x['grade']:<5} | {rs}")


def _cmd_backtest(args) -> None:
    from .backtest.engine import run_backtest
    from .research.universe import eligibility_panel, universe_symbols

    repo = open_repository()
    try:
        universe = None
        eligibility = None
        if args.universe:
            universe = universe_symbols(repo, args.universe)
            print(f"股票池 {args.universe}: {len(universe)} 只")
            if args.exclude_untradable:
                eligibility = eligibility_panel(repo, universe, max_suspend_days=args.max_suspend_days)
                print(f"剔除规则: ST/退市名称 + 连续停牌>={args.max_suspend_days}交易日 + 上市<60交易日")
        res = run_backtest(repo, top_k=args.top_k, cost_rate=args.cost,
                           rebalance=args.rebalance, hold_buffer=args.buffer,
                           strategy=args.strategy, market_filter=args.market_filter,
                           weighting=args.weighting, vol_target=args.vol_target,
                           start_date=args.start, end_date=args.end,
                           universe=universe, eligibility=eligibility,
                           max_weight=args.max_weight, max_sector=args.max_sector,
                           cost_model=args.cost_model, limit_model=args.limit_model,
                           universe_tag=args.universe)
    finally:
        repo.close()
    m = res["metrics"]
    print(f"\n===== 回测结果 {res['run_id']} =====")
    print(f"区间        : {res['start']} ~ {res['end']}  ({res['strategy']}, top_k={res['top_k']}, {res['rebalance']}, buffer={res['hold_buffer']:.0f}, 择时={'on' if args.market_filter else 'off'})")
    print(f"累计收益    : {m['total_return']:.2%}")
    print(f"年化收益    : {m['annual_return']:.2%}")
    print(f"年化波动    : {m['annual_vol']:.2%}")
    print(f"夏普        : {m['sharpe']:.2f}")
    print(f"最大回撤    : {m['max_drawdown']:.2%}")
    print(f"卡玛        : {m['calmar']:.2f}")
    print(f"日胜率      : {m['win_rate']:.2%}")
    print(f"基准年化    : {m['benchmark_annual']:.2%}")
    print(f"年化超额    : {m['excess_annual']:.2%}")


def _cmd_ic(args) -> None:
    from .backtest.metrics import factor_ic

    factors = ["ret_5", "ret_20", "boll_pos", "rsi14", "macd_hist", "vol_20",
               "main_net_ratio_5d", "inflow_days_5", "institution_trend"]
    repo = open_repository()
    print(f"\n===== 因子 IC（未来 {args.horizon} 日）=====")
    print(f"  {'因子':<22}{'mean_IC':>10}{'ICIR':>8}{'胜率':>9}{'n':>6}")
    try:
        for f in factors:
            r = factor_ic(repo, f, args.horizon)
            if r:
                print(f"  {r['factor']:<22}{r['mean_ic']:>10.4f}{r['icir']:>8.2f}{r['win_rate']:>9.2%}{r['n']:>6}")
    finally:
        repo.close()


def _cmd_tune(args) -> None:
    from .backtest.engine import run_backtest

    repo = open_repository()
    print(f"\n===== 参数扫描（成本 {args.cost:.4f}）=====")
    print(f"  {'strategy':>11} {'mkt':>4} {'top_k':>5} {'rebalance':>10} {'buffer':>7} {'年化':>9} {'夏普':>7} {'回撤':>9} {'超额':>9}")
    rows = []
    try:
        for strat in ("composite", "flow_rules", "inverted", "defensive"):
            for mf in (False, True):
                for top_k in (5, 8, 12, 20):
                    for reb in ("weekly", "monthly"):
                        for buf in (0.0, 1.0):
                            res = run_backtest(repo, top_k=top_k, cost_rate=args.cost,
                                               rebalance=reb, hold_buffer=buf,
                                               strategy=strat, market_filter=mf,
                                               weighting="vol_inv" if strat == "defensive" else "equal",
                                               vol_target=0.25 if strat == "defensive" else 0.0,
                                               persist=False)
                            m = res["metrics"]
                            rows.append((m.get("sharpe", float("-inf")), strat, mf, top_k, reb, buf, m))
                            print(f"  {strat:>11} {'on' if mf else 'off':>4} {top_k:>5} {reb:>10} {buf:>7.0f} "
                                  f"{m['annual_return']:>8.2%} {m['sharpe']:>7.2f} {m['max_drawdown']:>8.2%} {m['excess_annual']:>8.2%}")
    finally:
        repo.close()
    best = max(rows, key=lambda x: x[0])
    m = best[6]
    print(f"\n按夏普最优：strategy={best[1]}, 择时={'on' if best[2] else 'off'}, top_k={best[3]}, "
          f"rebalance={best[4]}, buffer={best[5]:.0f} "
          f"→ 年化 {m['annual_return']:.2%}, 夏普 {m['sharpe']:.2f}, 回撤 {m['max_drawdown']:.2%}")


def _cmd_incremental(args) -> None:
    from .ingest.incremental import run_incremental

    repo = open_repository()
    ds = get_source()
    try:
        stats = run_incremental(ds, repo, args.lookback)
    finally:
        repo.close()
    print("\n===== 增量拉取 =====")
    for k, v in stats.items():
        print(f"  {k:<14} {v}")


def _cmd_realtime(_args) -> None:
    from .predict.intraday import intraday_snapshot

    ds = get_source()
    data = intraday_snapshot(ds)
    print(f"\n===== 盘中快照 {data.get('ts')} =====")
    for x in data.get("rows", []):
        mn = (x["main_net"] or 0) / 1e8
        print(f"  [{x['sector']}] {x['symbol']} {x['name']:<5} 最新={x['last']} 涨跌={x['pct_chg']}% 主力净额={mn:+.3f}亿")


def _cmd_flowbackfill(args) -> None:
    from .ingest.backfill import backfill_fund_flow

    repo = open_repository()
    try:
        stats = backfill_fund_flow(repo, sleep=args.sleep, max_symbols=args.limit)
    finally:
        repo.close()
    print("\n===== 资金流回补 =====")
    for k, v in stats.items():
        print(f"  {k:<10} {v}")


def _cmd_research(args) -> None:
    import pandas as pd
    from .research.factor_diag import run_diagnostics, write_report

    df = run_diagnostics(horizon=args.horizon)
    if df.empty:
        print("无诊断结果")
        return
    pd.set_option("display.width", 240)
    cols = ["factor", "mean_ic", "icir", "win_rate", "n", "bull_ic", "bear_ic", "q1", "q5", "q5_q1"]
    print("\n===== 因子诊断（未来 %d 日）=====" % args.horizon)
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    p = write_report(df, args.horizon)
    print(f"\n报告已写入: {p}")


def _cmd_lab(args) -> None:
    import pandas as pd
    from .research.strategy_lab import load_panel, search, OOS_START, OOS_END

    repo = open_repository()
    try:
        panel = load_panel(repo)
        df = search(repo, panel,
                    score_names=args.scores.split(",") if args.scores else None,
                    top_ks=tuple(int(x) for x in args.top_ks.split(",")),
                    rebalances=tuple(args.rebalances.split(",")),
                    weightings=tuple(args.weightings.split(",")),
                    basket_trends=tuple(int(x) for x in args.trends.split(",")),
                    min_amounts=tuple(float(x) for x in args.min_amounts.split(",")))
    finally:
        repo.close()
    if df.empty:
        print("无结果")
        return
    pd.set_option("display.width", 260)
    pd.set_option("display.max_rows", 80)
    cols = ["score", "min_amt", "top_k", "reb", "wt", "trend",
            "ann", "sharpe", "dd", "excess", "oos_ann", "oos_sharpe", "oos_dd", "oos_excess"]
    print(f"\n===== 策略实验室（样本外 {OOS_START} ~ {OOS_END}）=====")
    print(df[cols].head(args.top).to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\n共 {len(df)} 组；完整结果见 {args.out}")
    df.to_csv(args.out, index=False, encoding="utf-8-sig")


def _cmd_portfolio(args) -> None:
    from .strategy.portfolio import build_target_portfolio

    repo = open_repository()
    try:
        res = build_target_portfolio(repo, trade_date=args.date, persist=not args.no_persist,
                                     strategy=args.strategy)
    finally:
        repo.close()
    if res.get("error"):
        print(res["error"])
        return
    action = {"rebalance": "调仓日（次一交易日收盘执行）",
              "hold": f"持有（距下次调仓 {res['days_to_next_rebalance']} 个交易日）",
              "initial": "首次建仓"}.get(res["action"], res["action"])
    print(f"\n===== 目标组合 {res['trade_date']} =====")
    print(f"策略        : {res['strategy']} | 执行: {res['execute_at']}")
    print(f"调仓状态    : {action}")
    bv = res["basket_vol_20d"]
    print(f"篮子波动    : {'--' if bv is None else f'{bv:.2%}'} | "
          f"波动缩放: {res['vol_scale']:.2f} | 总仓位: {res['exposure']:.2%}")
    print(f"\n  {'#':>2} | {'代码':<6} | {'名称':<8} | {'板块':<8} | {'综合分':>6} | {'波动':>7} | {'权重':>7} | 备注")
    print("  " + "-" * 92)
    for i, p in enumerate(res["positions"], 1):
        vol = "--" if p["vol_20"] is None else f"{p['vol_20']:.2%}"
        print(f"  {i:>2} | {p['symbol']:<6} | {p['name']:<8} | {p['sector']:<8} | "
              f"{p['score']:>6.2f} | {vol:>7} | {p['weight']:>7.2%} | {p['note']}")

    ch = res["changes"]
    hint = "，仅供下次调仓参考" if res["action"] == "hold" else ""
    print(f"\n相对上次调仓（{res['previous_rebalance'] or '无'}）{hint}：")
    if ch["type"] == "initial":
        print("  首次建仓，无历史对比")
    else:
        print(f"  新增: {', '.join(x['name'] or x['symbol'] for x in ch['added']) or '无'}")
        print(f"  剔除: {', '.join(x['name'] or x['symbol'] for x in ch['removed']) or '无'}")
        if ch["weight_changes"]:
            for x in ch["weight_changes"]:
                print(f"  调权: {x['name'] or x['symbol']}({x['symbol']}) {x['from']:.2%} → {x['to']:.2%}")
        else:
            print("  调权: 无（变动均小于 0.5%）")
    print("\n> 以上为策略目标组合与调仓清单，不构成投资建议。")


def _cmd_tracking(args) -> None:
    from .strategy.portfolio import STRATEGY_NAMES
    from .strategy.tracking import simulate_tracking

    repo = open_repository()
    try:
        name = STRATEGY_NAMES.get(args.strategy, args.strategy)
        res = simulate_tracking(repo, strategy=name, start_date=args.start,
                                persist=not args.no_persist)
    finally:
        repo.close()
    if res.get("status") == "waiting":
        print("\n===== 模拟跟踪 =====")
        print(res.get("message", "等待数据"))
        if res.get("baseline_date"):
            print(f"基线日: {res['baseline_date']}")
        return
    m = res["metrics"]
    print(f"\n===== 模拟跟踪 {res['strategy']} =====")
    print(f"区间        : {res['start_date']} ~ {res['last_date']}（{res['days']} 个交易日）")
    print(f"累计收益    : {m['total_return']:.2%}")
    print(f"年化收益    : {m['annual_return']:.2%}")
    print(f"年化波动    : {m['annual_vol']:.2%}")
    print(f"夏普        : {m['sharpe']:.2f}")
    print(f"最大回撤    : {m['max_drawdown']:.2%}")
    print(f"基准年化    : {m['benchmark_annual']:.2%}")
    print(f"年化超额    : {m['excess_annual']:.2%}")
    te = res.get("tracking_error")
    print(f"跟踪误差    : {'--' if te is None else f'{te:.2%}'}")
    print(f"调仓次数    : {len(res['rebalances'])}（{', '.join(res['rebalances'])}）")
    print("\n最近 5 个交易日：")
    print(f"  {'日期':<12}{'净值':>9}{'基准':>9}{'回撤':>9}{'仓位':>8}")
    for x in res["series"][-5:]:
        print(f"  {x['trade_date']:<12}{x['nav']:>9.4f}{x['benchmark']:>9.4f}"
              f"{x['drawdown']:>9.2%}{x['exposure']:>8.1%}")
    if res.get("pending_rebalance"):
        print(f"\n待执行调仓：{res['pending_rebalance']}（等待次一交易日行情）")


def _cmd_daily(args) -> None:
    from .pipeline.daily import run_daily_pipeline

    res = run_daily_pipeline(force=args.force, lookback_days=args.lookback)
    print("\n===== 每日流水线 =====")
    print(f"日期        : {res.get('date')}")
    print(f"状态        : {res.get('status')}")
    if res.get("reason"):
        print(f"原因        : {res['reason']}")
    if res.get("error"):
        print(f"错误        : {res['error']}")
    for k, v in res.get("steps", {}).items():
        print(f"  {k:<12} {v}")


def _cmd_recover(args) -> None:
    from .pipeline.recover import run_recovery

    res = run_recovery(lookback_days=args.lookback,
                       skip_ingest=args.no_ingest, dry_run=args.dry_run)
    print("\n===== 中断恢复 =====")
    print(f"日期        : {res.get('date')}")
    print(f"状态        : {res.get('status')}")
    if res.get("error"):
        print(f"错误        : {res['error']}")
    print(f"行情        : {res.get('quote_before')} -> {res.get('quote_after')}")
    print(f"最后快照    : {res.get('last_snapshot')}")
    miss = res.get("missing_rebalances") or []
    print(f"缺失调仓日  : {'无' if not miss else ', '.join(miss)}")
    for x in res.get("replayed", []):
        print(f"  已补 {x['date']}  {x['action']}  {x['positions']} 只")
    for k, v in res.get("steps", {}).items():
        print(f"  {k:<12} {v}")


def _cmd_universe(args) -> None:
    from .research.universe import (
        c1_csv_path, list_universes, prune_universe, refresh_c1, refresh_industry_universe,
        suspension_report, universe_members,
    )

    repo = open_repository()
    try:
        if args.refresh:
            if args.id == "C1":
                n = refresh_c1(repo)
                print(f"C1 已从 {c1_csv_path()} 刷新：{n} 只")
            elif args.id in ("C2", "C3"):
                stats = refresh_industry_universe(repo, args.id)
                print(f"\n{args.id} 行业池刷新：{stats.get('total', 0)} 只")
                for k, v in stats.items():
                    if k != "total":
                        print(f"  {k:<8} 原始 {v.get('raw', 0):<4} 保留 {v.get('kept', 0)}")
            else:
                print("仅支持 C1/C2/C3 刷新")
                return

        if args.prune and not args.sync:
            removed = prune_universe(repo, args.id, args.max_stale_days)
            print(f"剔除无行情/退市标的：{len(removed)} 只" +
                  ("" if not removed else " -> " + ", ".join(
                      f"{x['symbol']}({x['last_trade'] or '无行情'})" for x in removed)))

        members = universe_members(repo, args.id)
        print(f"\n===== 股票池 {args.id} =====")
        print(f"成员数: {len(members)}")
        if members:
            print("成员  : " + ", ".join(f"{s}({sec})" for s, sec in sorted(members.items())))

        meta = {r["symbol"]: r for r in repo.query("SELECT symbol, name, out_date FROM dim_stock")}
        st = [s for s in members if
              "ST" in (meta[s]["name"] if s in meta and meta[s]["name"] else "") or
              "退" in (meta[s]["name"] if s in meta and meta[s]["name"] else "")]
        delisted = [s for s in members if s in meta and (meta[s]["out_date"] or "")]
        print(f"ST/退市名称剔除: {', '.join(sorted(set(st))) or '无'}")
        print(f"已退市剔除    : {', '.join(sorted(set(delisted))) or '无'}")

        susp = suspension_report(repo, list(members), max_suspend_days=args.max_suspend_days)
        print(f"长期停牌(>={args.max_suspend_days}交易日): " +
              (", ".join(f"{x['symbol']}({x['gap_days']}日)" for x in susp) or "无"))

        if args.refresh and args.id == "C1":
            print("\n股票池清单:")
            for u in list_universes(repo):
                print(f"  {u['universe_id']:<4} {u['name']:<16} {u['members']} 只")

        if args.sync:
            from .ingest.fundamentals import sync_fundamentals
            from .ingest.universe_sync import sync_symbols

            symbols = list(members.keys())
            if not symbols:
                print("股票池为空，跳过补数")
                return
            print(f"\n开始补拉 {len(symbols)} 只的行情/资金流…")
            stats = sync_symbols(repo, symbols, start_date=args.start,
                                 with_flow=not args.skip_flow)
            print("\n===== 行情/资金流补数 =====")
            for k, v in stats.items():
                print(f"  {k:<12} {v}")
            if args.fundamentals:
                from .research.universe import is_etf

                equity = [s for s in symbols if not is_etf(s)]
                skipped = [s for s in symbols if is_etf(s)]
                if skipped:
                    print(f"\n跳过 ETF/LOF 财务同步（无财报）: {', '.join(skipped)}")
                fs = sync_fundamentals(repo, equity, start_year=args.start_year)
                print("\n===== 基本面同步 =====")
                for k, v in fs.items():
                    print(f"  {k:<12} {v}")
            if args.factors:
                from .features.engine import RESEARCH_FACTORS, compute_all
                from .features.fundamentals import compute_fundamental_factors

                print("\n重算因子（v2 + 基本面 v3）…")
                if args.id in ("C1", "C2", "C3"):
                    print("  技术/资金流:", sum(compute_all(
                        repo, include_flow=False, keep_factors=RESEARCH_FACTORS).values()), "行（lite）")
                else:
                    print("  技术/资金流:", sum(compute_all(repo).values()), "行")
                print("  基本面     :", sum(compute_fundamental_factors(repo).values()), "行")
            if args.prune:
                removed = prune_universe(repo, args.id, args.max_stale_days)
                print(f"\n剔除无行情/退市标的：{len(removed)} 只" +
                      ("" if not removed else " -> " + ", ".join(
                          f"{x['symbol']}({x['last_trade'] or '无行情'})" for x in removed)))
    finally:
        repo.close()


def _cmd_fundamentals(args) -> None:
    from .ingest.fundamentals import sync_fundamentals
    from .research.universe import C0, C1, is_etf, universe_symbols

    repo = open_repository()
    try:
        if args.symbols:
            symbols = [s.strip().zfill(6) for s in args.symbols.replace("，", ",").split(",") if s.strip()]
        else:
            symbols = list(dict.fromkeys(universe_symbols(repo, C0) + universe_symbols(repo, C1)))
        skipped = [s for s in symbols if is_etf(s)]
        symbols = [s for s in symbols if not is_etf(s)]
        if skipped:
            print(f"跳过 ETF/LOF（无财报）: {', '.join(skipped)}")
        if not symbols:
            print("无标的可同步")
            return
        print(f"基本面同步 {len(symbols)} 只，区间 {args.start_year}~{args.end_year or '今年'}")
        stats = sync_fundamentals(repo, symbols, start_year=args.start_year,
                                  end_year=args.end_year,
                                  with_dividend=not args.no_dividend,
                                  with_basic=not args.no_basic)
    finally:
        repo.close()
    print("\n===== 基本面同步 =====")
    for k, v in stats.items():
        print(f"  {k:<12} {v}")


def _cmd_attribution(args) -> None:
    from pathlib import Path

    from .research.attribution import format_tables, run_attribution

    repo = open_repository()
    try:
        res = run_attribution(repo, universe_id=args.universe, top_k=args.top_k,
                              cost_model=args.cost_model, limit_model=args.limit_model)
    finally:
        repo.close()
    if res.get("error"):
        print(res["error"])
        return
    yearly, sector, stocks = format_tables(res)
    out = Path("docs")
    out.mkdir(parents=True, exist_ok=True)
    yearly.to_csv(out / "attribution_yearly.csv", index=False, encoding="utf-8-sig")
    sector.to_csv(out / "attribution_sector.csv", encoding="utf-8-sig")
    stocks.to_csv(out / "attribution_stock.csv", encoding="utf-8-sig")

    print(f"\n===== 收益归因（{args.universe}，B3 / Top{args.top_k} / 月频）=====")
    print(f"{'年份':<8}{'净收益':>10}{'毛贡献':>10}{'成本拖累':>10}")
    for r in yearly.itertuples():
        print(f"{r.year:<8}{r.net_return:>10.2%}{r.gross_contrib:>10.2%}{r.cost_drag:>10.2%}")

    sec_total = sector.sum(axis=1).sort_values(ascending=False)
    print("\n行业累计贡献（毛）：")
    for sec, v in sec_total.items():
        print(f"  {sec:<12}{v:>9.2%}")

    top = res["stock_total"]
    print("\n个股累计贡献 Top10：")
    for sym, v in top.head(10).items():
        print(f"  {sym}  {v:>9.2%}")
    print("\n个股累计贡献 Bottom10：")
    for sym, v in top.tail(10).items():
        print(f"  {sym}  {v:>9.2%}")
    print(f"\n结果已写入 {out}\\attribution_*.csv")


def _cmd_monitor(args) -> None:
    from pathlib import Path

    from .research.monitor import run_drawdown_monitor, run_ic_monitor

    repo = open_repository()
    try:
        rows, res = None, None
        if args.stage == "drawdown":
            rows = run_drawdown_monitor(repo, dd_alert=args.dd_alert)
        else:
            res = run_ic_monitor(repo, universe_id=args.universe, horizon=args.horizon,
                                 window=args.window, start=args.start)
    finally:
        repo.close()

    if args.stage == "drawdown":
        print(f"\n===== 策略回撤监控（告警阈值 {args.dd_alert:.0%}）=====")
        for x in rows:
            if x["status"] == "no_data":
                print(f"  {x['kind']:<20} {x['strategy']:<36} {x['message']}")
                continue
            print(f"  {x['kind']:<20} {x['strategy']:<36} {x['last_date']}  "
                  f"净值 {x['nav']:.4f}  回撤 {x['drawdown']:>7.2%}  "
                  f"近端 {x['recent_return']:>7.2%}  {x['status']}")
        return

    if res.get("error"):
        print(res["error"])
        return
    s = res["summary"]
    out = Path("docs/factor_ic_monitor.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    s.to_csv(out, index=False, encoding="utf-8-sig")

    print(f"\n===== 因子 IC 监控（{args.universe}，horizon={args.horizon}，"
          f"近端窗口={args.window} 日）=====")
    print(f"{'因子':<18}{'全期IC':>9}{'全期ICIR':>10}{'近端IC':>9}{'近端ICIR':>10}"
          f"{'衰减比':>8}  状态")
    for r in s.itertuples():
        print(f"{r.factor:<18}{r.full_mean_ic:>9.3f}{r.full_icir:>10.2f}"
              f"{r.recent_mean_ic:>9.3f}{r.recent_icir:>10.2f}{r.decay_ratio:>8.2f}  "
              f"{r.status}{(' · ' + r.note) if r.note else ''}")
    if res["alerts"]:
        print(f"\n告警 {len(res['alerts'])} 项：")
        for a in res["alerts"]:
            print(f"  [{a['status']}] {a['factor']}：{a['note']}（全期 {a['full_mean_ic']:.3f} "
                  f"→ 近端 {a['recent_mean_ic']:.3f}）")
    else:
        print("\n无 IC 衰减告警")
    print(f"\n结果已写入 {out}")


def _cmd_revisions(args) -> None:
    from .ingest.fundamentals import backfill_financial_revisions, revision_report

    repo = open_repository()
    try:
        if args.backfill:
            n = backfill_financial_revisions(repo)
            print(f"已回补 {n} 条版本记录（version=1）")
        rep = revision_report(repo)
    finally:
        repo.close()
    print("\n===== 财务修订版本 =====")
    print(f"版本行数        : {rep['rows']}")
    print(f"多版本报告期数  : {rep['keys_multi_version']}")
    if rep["recent_revisions"]:
        print("\n最近修订：")
        for r in rep["recent_revisions"]:
            print(f"  {r['symbol']} {r['stat_date']} {r['metric']:<18} "
                  f"v{r['version']} 值={r['value']} 公告日={r['pub_date']}")


def _cmd_experiments(args) -> None:
    from .research.experiment import (
        run_ablation, run_lean, run_portfolio, run_risk, run_round2,
        run_universe_compare, run_walkforward, write_csv,
    )
    from .research.regime import run_regime
    from .research.sensitivity import run_cost_sensitivity, run_sensitivity
    from .research.variants import run_variants

    cols = ["experiment", "annual", "vol", "sharpe", "max_dd", "calmar", "excess",
            "cost", "ann_in", "ann_out", "sharpe_in", "sharpe_out"]
    stages = (["b", "a", "lean", "portfolio", "risk", "sensitivity", "cost",
               "regime", "variants", "universes", "walkforward"]
              if args.stage == "all" else [args.stage])
    suffix = "" if args.universe == "C0" else f"_{args.universe}"
    repo = open_repository()
    try:
        for stage in stages:
            dsr = None
            if stage == "b":
                df = run_round2(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_round2{suffix}.csv")
                title = "第二轮：防御增强因子增量"
            elif stage == "a":
                df = run_ablation(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_ablation{suffix}.csv")
                title = "第一轮：因子消融与行业中性"
            elif stage == "lean":
                df = run_lean(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_lean{suffix}.csv")
                title = "探索：估值版精简因子集"
            elif stage == "portfolio":
                df = run_portfolio(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_portfolio{suffix}.csv")
                title = "第三轮：组合构建"
            elif stage == "risk":
                df = run_risk(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_risk{suffix}.csv")
                title = "第四轮：风险参数"
            elif stage == "sensitivity":
                res = run_sensitivity(repo, args.universe, args.cost_model, args.limit_model)
                df = res["table"]
                dsr = res["dsr"]
                p = write_csv(df, f"docs/experiments_sensitivity{suffix}.csv")
                title = "参数敏感性与多重检验"
            elif stage == "cost":
                df = run_cost_sensitivity(repo, args.universe, args.limit_model)
                p = write_csv(df, f"docs/experiments_cost{suffix}.csv")
                title = "交易成本敏感性"
            elif stage == "regime":
                df = run_regime(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_regime{suffix}.csv")
                print("\n===== 市场状态分组（上行/震荡/下行）=====")
                print(df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
                print(f"\n完整结果: {p}")
                continue
            elif stage == "variants":
                df = run_variants(repo, args.universe, args.cost_model, args.limit_model)
                p = write_csv(df, f"docs/experiments_variants{suffix}.csv")
                title = "策略变体：平滑/动态TopK/动态波动目标"
            elif stage == "universes":
                df = run_universe_compare(repo, ("C0", "C1", "C2", "C3"),
                                          args.cost_model, args.limit_model)
                p = write_csv(df, "docs/experiments_universes.csv")
                title = "股票池对比 C0/C1/C2/C3"
            else:
                wf = run_walkforward(repo, args.universe, args.cost_model, args.limit_model)
                print("\n===== 第五轮：滚动前推（每年用过去3年夏普选 TopK）=====")
                if wf.get("error"):
                    print(wf["error"])
                else:
                    m = wf["metrics"]
                    print(f"区间 {wf['nav'].index[0]} ~ {wf['nav'].index[-1]}，"
                          f"年化 {m['annual_return']:.2%}，夏普 {m['sharpe']:.2f}，"
                          f"回撤 {m['max_drawdown']:.2%}")
                    for x in wf["picks"]:
                        print(f"  {x['year']} 选择 {x['pick']}（训练夏普 {x['train_sharpe']:.2f}）")
                continue

            print(f"\n===== {title} =====")
            show = [c for c in cols if c in df.columns]
            print(df[show].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
            if dsr:
                print(f"\nDeflated Sharpe: {dsr['dsr']:.3f}"
                      f"（基线夏普 {dsr['sharpe_annual']:.2f} vs 门槛 {dsr['sr0_annual']:.2f}，"
                      f"试验 {dsr['n_trials']} 次，样本 {dsr['n_obs']} 日）")
            print(f"\n完整结果（含分年度收益）: {p}")
    finally:
        repo.close()


def _cmd_scheduler(args) -> None:
    from .pipeline.scheduler import run_scheduler

    run_scheduler(target_time=args.time, force=args.force)


def _cmd_status(_args) -> None:
    repo = open_repository()
    print("===== 数据水位 =====")
    for row in repo.query("SELECT dataset, last_date, last_run_at, status FROM ingest_watermark"):
        print(f"  {row['dataset']:<18} {row['last_date']}  ({row['status']})")
    print("===== 行数 =====")
    for t in ["dim_stock", "dim_sector", "dim_sector_member", "fact_quote", "fact_stock_flow", "fact_sector_flow"]:
        print(f"  {t:<20} {repo.count(t)}")
    repo.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="stock_cash_streaming")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="历史初始化")
    p_init.add_argument("--start", default="20200101")
    p_init.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    p_init.add_argument("--limit", type=int, default=None, help="每板块最多处理多少只（调试用）")
    p_init.set_defaults(func=_cmd_init)

    p_seed = sub.add_parser("seed", help="载入 MCP 种子数据（受限环境聚合方案）")
    p_seed.set_defaults(func=_cmd_seed)

    p_factors = sub.add_parser("factors", help="计算并落库全部因子")
    p_factors.add_argument("--no-fundamentals", action="store_true",
                           help="跳过基本面因子（v3）计算")
    p_factors.add_argument("--lite", action="store_true",
                           help="研究模式：只算 9 个技术因子且跳过资金流")
    p_factors.set_defaults(func=_cmd_factors)

    p_signals = sub.add_parser("signals", help="生成板块内综合信号")
    p_signals.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，默认取最新")
    p_signals.set_defaults(func=_cmd_signals)

    p_bt = sub.add_parser("backtest", help="运行策略回测")
    p_bt.add_argument("--top-k", type=int, default=12, help="每日持仓数量")
    p_bt.add_argument("--cost", type=float, default=0.0013, help="单边换手成本率")
    p_bt.add_argument("--rebalance", choices=["daily", "weekly", "monthly"], default="monthly",
                      help="再平衡频率")
    p_bt.add_argument("--buffer", type=float, default=1.0, help="持仓排名缓冲（降低换手）")
    p_bt.add_argument("--strategy", choices=["composite", "flow_rules", "inverted", "defensive",
                                             "defensive_enhanced", "defensive_valuation"],
                      default="defensive",
                      help="策略：defensive=防御型（生产默认）；defensive_valuation=防御+估值（候选）；"
                           "defensive_enhanced=完整增强；composite/flow_rules/inverted=研究用")
    p_bt.add_argument("--weighting", choices=["equal", "score", "rank", "vol_inv"], default="vol_inv",
                      help="持仓加权方式")
    p_bt.add_argument("--vol-target", type=float, default=0.25, help="年化波动率目标（0=关闭）")
    p_bt.add_argument("--market-filter", action="store_true", help="开启沪深300 200日均线择时")
    p_bt.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD")
    p_bt.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    p_bt.add_argument("--universe", default=None, help="股票池 ID（C0/C1...）；默认全部行情标的")
    p_bt.add_argument("--exclude-untradable", action="store_true",
                      help="剔除 ST/退市、长期停牌、上市不足的标的（需配合 --universe）")
    p_bt.add_argument("--max-suspend-days", type=int, default=60, help="长期停牌阈值（交易日）")
    p_bt.add_argument("--cost-model", choices=["flat", "a_share"], default="flat",
                      help="成本模型：flat=固定单边费率；a_share=历史印花税+佣金+过户费+滑点")
    p_bt.add_argument("--limit-model", choices=["flat", "board"], default="flat",
                      help="涨跌停阈值：flat=统一9.8%%；board=按主板/创业板/科创板/北交所")
    p_bt.add_argument("--max-weight", type=float, default=1.0, help="单股权重上限（如 0.10）")
    p_bt.add_argument("--max-sector", type=float, default=1.0, help="行业权重上限（如 0.30）")
    p_bt.set_defaults(func=_cmd_backtest)

    p_tune = sub.add_parser("tune", help="策略参数扫描")
    p_tune.add_argument("--cost", type=float, default=0.0013, help="单边换手成本率")
    p_tune.set_defaults(func=_cmd_tune)

    p_inc = sub.add_parser("incremental", help="每日增量拉取")
    p_inc.add_argument("--lookback", type=int, default=10, help="回看天数")
    p_inc.set_defaults(func=_cmd_incremental)

    p_rt = sub.add_parser("realtime", help="拉取一次盘中快照")
    p_rt.set_defaults(func=_cmd_realtime)

    p_fb = sub.add_parser("flowbackfill", help="回补个股资金流长历史（新浪）")
    p_fb.add_argument("--sleep", type=float, default=0.4, help="每只股票间隔秒数")
    p_fb.add_argument("--limit", type=int, default=None, help="最多处理多少只（调试）")
    p_fb.set_defaults(func=_cmd_flowbackfill)

    p_ic = sub.add_parser("ic", help="计算因子 IC")
    p_ic.add_argument("--horizon", type=int, default=5, help="未来收益天数")
    p_ic.set_defaults(func=_cmd_ic)

    p_res = sub.add_parser("research", help="因子诊断（分年度/分 regime）")
    p_res.add_argument("--horizon", type=int, default=5, help="未来收益天数")
    p_res.set_defaults(func=_cmd_research)

    p_status = sub.add_parser("status", help="查看数据水位与行数")
    p_status.set_defaults(func=_cmd_status)

    p_pf = sub.add_parser("portfolio", help="生成目标组合与调仓清单（实盘跟踪）")
    p_pf.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，默认最新")
    p_pf.add_argument("--strategy", choices=["defensive", "defensive_valuation"],
                      default="defensive", help="defensive=生产口径；defensive_valuation=候选（+8%%估值）")
    p_pf.add_argument("--no-persist", action="store_true", help="不写入目标组合历史")
    p_pf.set_defaults(func=_cmd_portfolio)

    p_tr = sub.add_parser("tracking", help="按调仓快照计算模拟跟踪净值")
    p_tr.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD，默认首个调仓快照")
    p_tr.add_argument("--strategy", choices=["defensive", "defensive_valuation"],
                      default="defensive", help="跟踪哪个策略的调仓快照")
    p_tr.add_argument("--no-persist", action="store_true", help="不写入跟踪净值历史")
    p_tr.set_defaults(func=_cmd_tracking)

    p_daily = sub.add_parser("daily", help="每日自动流水线（增量→因子→信号→组合→跟踪）")
    p_daily.add_argument("--force", action="store_true", help="非交易日/行情未更新时也强制执行")
    p_daily.add_argument("--lookback", type=int, default=10, help="增量回看天数")
    p_daily.set_defaults(func=_cmd_daily)

    p_rec = sub.add_parser("recover", help="异常中断恢复：补行情、补漏调仓快照、重放跟踪")
    p_rec.add_argument("--lookback", type=int, default=30,
                       help="行情回看天数，需覆盖中断天数（默认 30）")
    p_rec.add_argument("--no-ingest", action="store_true",
                       help="跳过行情补拉（仅重算因子与重放快照）")
    p_rec.add_argument("--dry-run", action="store_true",
                       help="只报告缺失调仓日，不写库")
    p_rec.set_defaults(func=_cmd_recover)

    p_uni = sub.add_parser("universe", help="股票池：查看 C0/C1、刷新 C1、补拉数据")
    p_uni.add_argument("--id", default="C0", help="股票池 ID（C0/C1/C2...）")
    p_uni.add_argument("--refresh", action="store_true", help="从 config/universe_c1.csv 刷新 C1")
    p_uni.add_argument("--sync", action="store_true", help="为成员补拉行情与资金流历史")
    p_uni.add_argument("--fundamentals", action="store_true", help="补数时一并同步基本面")
    p_uni.add_argument("--factors", action="store_true", help="补数后重算因子（含基本面 v3）")
    p_uni.add_argument("--skip-flow", action="store_true", help="补数时跳过个股资金流（行业池研究可跳过）")
    p_uni.add_argument("--prune", action="store_true",
                       help="剔除无行情/长期无行情成员（建议补数完成后执行）")
    p_uni.add_argument("--max-stale-days", type=int, default=365,
                       help="无行情超过多少天视为退市/长期停牌")
    p_uni.add_argument("--start", default="20180101", help="行情起始日 YYYYMMDD")
    p_uni.add_argument("--start-year", type=int, default=2017, help="基本面起始年份")
    p_uni.add_argument("--max-suspend-days", type=int, default=60, help="长期停牌阈值（交易日）")
    p_uni.set_defaults(func=_cmd_universe)

    p_fund = sub.add_parser("fundamentals", help="同步财务/分红/上市退市数据")
    p_fund.add_argument("--symbols", default=None, help="逗号分隔代码；默认 C0+C1")
    p_fund.add_argument("--start-year", type=int, default=2017)
    p_fund.add_argument("--end-year", type=int, default=None)
    p_fund.add_argument("--no-dividend", action="store_true")
    p_fund.add_argument("--no-basic", action="store_true")
    p_fund.set_defaults(func=_cmd_fundamentals)

    p_rev = sub.add_parser("revisions", help="财务修订版本回补与查看")
    p_rev.add_argument("--backfill", action="store_true",
                       help="把现有 fact_financial 快照初始化为 version=1")
    p_rev.set_defaults(func=_cmd_revisions)

    p_mon = sub.add_parser("monitor", help="因子/策略运行监控")
    p_mon.add_argument("--stage", choices=["ic", "drawdown"], default="ic",
                       help="ic=滚动 IC 与衰减告警；drawdown=策略回撤告警")
    p_mon.add_argument("--universe", default="C0", help="股票池 ID（默认 C0）")
    p_mon.add_argument("--horizon", type=int, default=5, help="未来收益天数")
    p_mon.add_argument("--window", type=int, default=120, help="近端统计窗口（交易日）")
    p_mon.add_argument("--dd-alert", type=float, default=-0.10,
                       help="回撤告警阈值（默认 -10%%）")
    p_mon.add_argument("--start", default="2018-01-01")
    p_mon.set_defaults(func=_cmd_monitor)

    p_attr = sub.add_parser("attribution", help="组合收益归因（年度/行业/个股）")
    p_attr.add_argument("--universe", default="C0")
    p_attr.add_argument("--top-k", type=int, default=12)
    p_attr.add_argument("--cost-model", choices=["flat", "a_share"], default="a_share")
    p_attr.add_argument("--limit-model", choices=["flat", "board"], default="board")
    p_attr.set_defaults(func=_cmd_attribution)

    p_exp = sub.add_parser("experiments", help="研究实验：因子消融/组合/风险/滚动验证")
    p_exp.add_argument("--universe", default="C0", help="股票池 ID（默认 C0）")
    p_exp.add_argument("--stage", default="b",
                       choices=["b", "a", "lean", "portfolio", "risk", "sensitivity",
                                "cost", "regime", "variants", "universes",
                                "walkforward", "all"],
                       help="b=因子增量；a=消融；lean=精简；portfolio=组合；risk=风险；"
                            "sensitivity=参数敏感性；cost=成本敏感性；regime=市场状态；"
                            "variants=策略变体；universes=股票池对比；walkforward=滚动验证")
    p_exp.add_argument("--cost-model", choices=["flat", "a_share"], default="a_share")
    p_exp.add_argument("--limit-model", choices=["flat", "board"], default="board")
    p_exp.set_defaults(func=_cmd_experiments)

    p_sch = sub.add_parser("scheduler", help="启动每日定时调度器（常驻进程）")
    p_sch.add_argument("--time", default="16:10", help="每周一至周五运行时间 HH:MM")
    p_sch.add_argument("--force", action="store_true", help="非交易日也执行")
    p_sch.set_defaults(func=_cmd_scheduler)

    p_lab = sub.add_parser("lab", help="策略实验室：批量搜索 + 样本内外评估")
    p_lab.add_argument("--scores", default=None, help="逗号分隔：core,core_sn,coremix,tech,all,volonly")
    p_lab.add_argument("--top-ks", default="5,8,12,20,30")
    p_lab.add_argument("--rebalances", default="weekly,monthly")
    p_lab.add_argument("--weightings", default="equal,score,vol_inv")
    p_lab.add_argument("--trends", default="0,60")
    p_lab.add_argument("--min-amounts", default="5e7,1e8,2e8")
    p_lab.add_argument("--top", type=int, default=30, help="打印前 N 行")
    p_lab.add_argument("--out", default="docs/research_strategy_lab.csv")
    p_lab.set_defaults(func=_cmd_lab)

    args = parser.parse_args(argv)
    setup_logging()
    args.func(args)


if __name__ == "__main__":
    main()

