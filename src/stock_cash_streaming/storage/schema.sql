-- 数据库结构（SQLite）
-- 定义见 docs/software-architecture.md §5.2

-- ===== 维度表 =====

-- 股票基础信息
CREATE TABLE IF NOT EXISTS dim_stock (
    symbol      TEXT PRIMARY KEY,           -- 如 600519 / 000001
    name        TEXT,
    market      TEXT,                       -- sh / sz
    industry    TEXT,
    is_st       INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'normal',
    ipo_date    TEXT,                       -- 上市日期（Baostock）
    out_date    TEXT,                       -- 退市日期（Baostock，空=仍在市）
    updated_at  TEXT
);

-- 股票池定义（C0/C1/C2/C3 ...）
CREATE TABLE IF NOT EXISTS dim_universe (
    universe_id        TEXT PRIMARY KEY,
    name               TEXT,
    description        TEXT,
    updated_at         TEXT,
    is_custom          INTEGER NOT NULL DEFAULT 0,   -- 1=用户自建池（可改名/增删成员）
    daily_enabled      INTEGER NOT NULL DEFAULT 0,   -- 1=纳入每日池任务（C0 由全局流水线维护）
    members_updated_at TEXT                          -- 成员最近变更时间（用于判断回测是否过期）
);

-- 股票池成员（快照；C0 由板块成分派生，C1 由用户 CSV 录入）
CREATE TABLE IF NOT EXISTS dim_universe_member (
    universe_id TEXT,
    symbol      TEXT,
    sector_code TEXT,
    source      TEXT,
    updated_at  TEXT,
    PRIMARY KEY (universe_id, symbol)
);

-- 板块
CREATE TABLE IF NOT EXISTS dim_sector (
    sector_code TEXT PRIMARY KEY,           -- 以东财板块名称为代码
    sector_name TEXT,
    sector_type TEXT,                       -- industry / concept
    updated_at  TEXT
);

-- 板块成分股（保留历史归属：out_date 为空表示当前成分）
CREATE TABLE IF NOT EXISTS dim_sector_member (
    sector_code TEXT,
    symbol      TEXT,
    in_date     TEXT,
    out_date    TEXT,
    PRIMARY KEY (sector_code, symbol, in_date)
);

-- ===== 事实表 =====

-- 个股日行情
CREATE TABLE IF NOT EXISTS fact_quote (
    symbol      TEXT,
    trade_date  TEXT,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    pct_chg     REAL,
    turnover    REAL,
    adj_factor  REAL,
    src         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (symbol, trade_date)
);

-- 板块资金流（日）
CREATE TABLE IF NOT EXISTS fact_sector_flow (
    sector_code TEXT,
    trade_date  TEXT,
    main_net    REAL,
    main_ratio  REAL,
    xl_net      REAL,   -- 超大单净流入
    l_net       REAL,   -- 大单净流入
    m_net       REAL,   -- 中单净流入
    s_net       REAL,   -- 小单净流入
    xl_ratio    REAL,
    l_ratio     REAL,
    m_ratio     REAL,
    s_ratio     REAL,
    src         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (sector_code, trade_date)
);

-- 个股资金流（日）
CREATE TABLE IF NOT EXISTS fact_stock_flow (
    symbol      TEXT,
    trade_date  TEXT,
    main_net    REAL,
    main_ratio  REAL,
    xl_net      REAL,
    l_net       REAL,
    m_net       REAL,
    s_net       REAL,
    xl_ratio    REAL,
    l_ratio     REAL,
    m_ratio     REAL,
    s_ratio     REAL,
    close       REAL,
    pct_chg     REAL,
    src         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (symbol, trade_date)
);

-- 因子值（带版本）
CREATE TABLE IF NOT EXISTS fact_factor (
    factor      TEXT,
    symbol      TEXT,
    trade_date  TEXT,
    version     TEXT,
    value       REAL,
    extra       TEXT,
    PRIMARY KEY (factor, symbol, trade_date, version)
);

-- 财务指标（point-in-time：pub_date 为实际公告日，选股只能使用 pub_date <= t 的记录）
CREATE TABLE IF NOT EXISTS fact_financial (
    symbol      TEXT,
    stat_date   TEXT,                   -- 报告期 YYYY-MM-DD
    pub_date    TEXT,                   -- 公告日 YYYY-MM-DD
    metric      TEXT,                   -- 指标名（如 net_profit / equity / roe ...）
    value       REAL,
    src         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (symbol, stat_date, metric)
);

-- 财务修订版本（append-only）：同一报告期指标每次数值/公告日变化都追加一条
CREATE TABLE IF NOT EXISTS fact_financial_revision (
    symbol      TEXT NOT NULL,
    stat_date   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    value       REAL,
    pub_date    TEXT,
    src         TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (symbol, stat_date, metric, version)
);

-- 分红（point-in-time：announce_date 为公告日）
CREATE TABLE IF NOT EXISTS fact_dividend (
    symbol          TEXT,
    announce_date   TEXT,
    ex_date         TEXT,
    cash_ps         REAL,               -- 每股现金分红（税前，元）
    stock_ps        REAL,               -- 每股送转股
    src             TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (symbol, announce_date, ex_date)
);

-- 信号
CREATE TABLE IF NOT EXISTS fact_signal (
    symbol      TEXT,
    trade_date  TEXT,
    phase       TEXT,                   -- eod / intraday
    score       REAL,
    grade       TEXT,
    reasons     TEXT,                   -- JSON
    PRIMARY KEY (symbol, trade_date, phase)
);

-- 回测
CREATE TABLE IF NOT EXISTS backtest_run (
    run_id       TEXT PRIMARY KEY,
    strategy     TEXT,
    params       TEXT,
    start_date   TEXT,
    end_date     TEXT,
    data_version TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS backtest_result (
    run_id      TEXT,
    trade_date  TEXT,
    nav         REAL,
    benchmark   REAL,
    drawdown    REAL,
    PRIMARY KEY (run_id, trade_date)
);

-- 目标组合（实盘跟踪）：组合级元数据
CREATE TABLE IF NOT EXISTS fact_target_run (
    trade_date    TEXT,
    strategy      TEXT,
    is_rebalance  INTEGER,
    basket_vol    REAL,
    vol_scale     REAL,
    exposure      REAL,
    created_at    TEXT,
    PRIMARY KEY (trade_date, strategy)
);

-- 目标组合（实盘跟踪）：个股权重
CREATE TABLE IF NOT EXISTS fact_target_portfolio (
    trade_date   TEXT,
    strategy     TEXT,
    symbol       TEXT,
    name         TEXT,
    sector_code  TEXT,
    score        REAL,
    vol_20       REAL,
    raw_weight   REAL,
    weight       REAL,
    is_rebalance INTEGER,
    note         TEXT,
    created_at   TEXT,
    PRIMARY KEY (trade_date, strategy, symbol)
);

-- 模拟跟踪净值（实盘跟踪表）
CREATE TABLE IF NOT EXISTS fact_tracking_nav (
    trade_date  TEXT,
    strategy    TEXT,
    nav         REAL,
    benchmark   REAL,
    exposure    REAL,
    drawdown    REAL,
    created_at  TEXT,
    PRIMARY KEY (trade_date, strategy)
);

-- ===== 运维表 =====

-- 数据水位（驱动增量）
CREATE TABLE IF NOT EXISTS ingest_watermark (
    dataset     TEXT PRIMARY KEY,
    last_date   TEXT,
    last_run_at TEXT,
    status      TEXT
);

-- 任务日志
CREATE TABLE IF NOT EXISTS task_log (
    task_id     TEXT PRIMARY KEY,
    name        TEXT,
    status      TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    message     TEXT
);

-- ===== 索引 =====
CREATE INDEX IF NOT EXISTS idx_quote_date       ON fact_quote(trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_flow_date  ON fact_stock_flow(trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_flow_date ON fact_sector_flow(trade_date);
CREATE INDEX IF NOT EXISTS idx_factor_date      ON fact_factor(trade_date);
CREATE INDEX IF NOT EXISTS idx_signal_date      ON fact_signal(trade_date);
CREATE INDEX IF NOT EXISTS idx_target_date      ON fact_target_portfolio(trade_date);
CREATE INDEX IF NOT EXISTS idx_fin_pub          ON fact_financial(symbol, pub_date);
CREATE INDEX IF NOT EXISTS idx_fin_rev          ON fact_financial_revision(symbol, stat_date);
CREATE INDEX IF NOT EXISTS idx_div_announce     ON fact_dividend(symbol, announce_date);
CREATE INDEX IF NOT EXISTS idx_universe_member  ON dim_universe_member(universe_id);
