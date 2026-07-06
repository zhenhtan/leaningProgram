"""
quant_demo.py — 最简单的 A股/港股量化交易接口示例
数据源: akshare（免费、无需注册）
演示内容:
  1. 获取历史 K 线数据
  2. 计算双均线（MA5 / MA20）
  3. 生成买卖信号
  4. 简单回测，统计收益
"""

import numpy as np
import pandas as pd
import yfinance as yf
import argparse


# ─────────────────────────────────────────────
# 1. 获取历史日 K 线
#    数据源：yfinance（雅虎财经，走企业代理均可）
#    A股代码映射：000001 → 000001.SS，600036 → 600036.SS
#    港股：0700.HK   美股：AAPL
#    若网络完全不通，自动生成合成数据用于演示
# ─────────────────────────────────────────────
def _make_synthetic(start: str, end: str) -> pd.DataFrame:
    """生成随机游走合成价格，用于离线演示。"""
    print("    ⚠ 网络不通，使用合成数据演示回测逻辑")
    rng = np.random.default_rng(42)
    dates = pd.bdate_range(start=start, end=end)
    n = len(dates)
    price = 30.0 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    volume = rng.integers(1_000_000, 5_000_000, n)
    df = pd.DataFrame({
        "open":   price * (1 + rng.uniform(-0.005, 0.005, n)),
        "close":  price,
        "high":   price * (1 + rng.uniform(0.002, 0.015, n)),
        "low":    price * (1 - rng.uniform(0.002, 0.015, n)),
        "volume": volume,
    }, index=dates)
    df.index.name = "date"
    return df


def _yf_symbol(symbol: str) -> str:
    """将常见 A 股/港股代码转换为 yfinance 格式。"""
    if symbol.isdigit():
        prefix = symbol[0]
        # 5/6/9 开头：上交所（含上交所 ETF，如 588xxx、510xxx）
        suffix = ".SS" if prefix in ("5", "6", "9") else ".SZ"
        return symbol + suffix
    return symbol   # 已带后缀或美股直接返回


def get_daily_data(symbol: str = "600036",   # 招商银行
                   start: str = "20230101",
                   end: str = "20241231") -> pd.DataFrame:
    """
    返回包含 open/close/high/low/volume 的 DataFrame，index 为日期。
    若 yfinance 请求失败，自动回退到合成数据。
    """
    try:
        ticker = _yf_symbol(symbol)
        df = yf.download(
            ticker,
            start=pd.Timestamp(start),
            end=pd.Timestamp(end) + pd.Timedelta(days=1),
            auto_adjust=True,
            progress=False,
        )
        if df.empty:
            raise ValueError(f"yfinance returned empty data for {ticker}")

        print(f"    成功获取 {len(df)} 行数据，日期范围 {df.index[0].date()} ~ {df.index[-1].date()}")
        # yfinance 返回多级列时展平（必须在 join 之前）
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        print(f"    数据列：{', '.join(df.columns)}")

        df.index.name = "date"
        df = df.sort_index()
        df = df.dropna(subset=["open", "close", "high", "low"])
        return df[["open", "close", "high", "low", "volume"]]

    except Exception as e:
        print(f"    yfinance 请求失败（{e}），切换合成数据")
        return _make_synthetic(start, end)


def calc_max_drawdown(equity_curve: pd.Series) -> tuple[float, pd.Timestamp | None]:
    """计算最大回撤金额及其发生日期（金额为正值，单位与资产一致）。"""
    if equity_curve.empty:
        return 0.0, None
    running_max = equity_curve.cummax()
    drawdown_amount = running_max - equity_curve
    max_dd_date = drawdown_amount.idxmax()
    return float(drawdown_amount.loc[max_dd_date]), max_dd_date


def calc_realized_unrealized_pnl(
    trades: pd.DataFrame,
    initial_shares: int,
    initial_cost_per_share: float,
    final_price: float,
) -> tuple[float, float, float, float]:
    """按平均成本法分解收益：已实现、未实现、合计与期末持仓平均成本。"""
    holdings = int(initial_shares)
    cost_basis_total = float(initial_shares) * float(initial_cost_per_share)
    realized_pnl = 0.0

    if not trades.empty:
        ordered = trades.sort_values("date")
        for row in ordered.itertuples(index=False):
            qty = int(row.shares)
            price = float(row.price)
            action = str(row.action).upper()

            if action == "BUY":
                holdings += qty
                cost_basis_total += qty * price
            elif action == "SELL":
                if holdings <= 0:
                    continue
                sell_qty = min(qty, holdings)
                avg_cost = cost_basis_total / holdings if holdings > 0 else 0.0
                realized_pnl += (price - avg_cost) * sell_qty
                holdings -= sell_qty
                cost_basis_total -= avg_cost * sell_qty

    avg_cost_end = (cost_basis_total / holdings) if holdings > 0 else 0.0
    unrealized_pnl = holdings * float(final_price) - cost_basis_total
    total_pnl = realized_pnl + unrealized_pnl
    return realized_pnl, unrealized_pnl, total_pnl, avg_cost_end


def backtest_benchmark_with_pre_sell(
    df: pd.DataFrame,
    init_cash: float,
    init_shares: int,
    pre_sell_price: float,
    pre_sell_ratio: float = 0.5,
) -> tuple[int, float, pd.Series, bool]:
    """基准策略：持有不做网格，不执行首次触达减仓。"""
    cash = init_cash
    shares = init_shares
    pre_sell_done = False
    equity_points = []

    for date, row in df.iterrows():
        close = float(row["close"])
        equity_points.append({"date": date, "equity": cash + shares * close})

    equity_curve = pd.DataFrame(equity_points).set_index("date")["equity"] if equity_points else pd.Series(dtype=float)
    return shares, cash, equity_curve, pre_sell_done


def get_mode_params(mode: str, init_shares: int) -> dict:
    """根据风险档位返回网格参数。"""
    profiles = {
        "conservative": {
            "grid_start_threshold": 1.65,
            "grid_step": 0.12,
            "trade_ratio": 0.05,
        },
        "normal": {
        # 回归测试从2023-07-01到2024-06-26，网格交易参数调整为更适合当前市场的设置
        # 截止2026-06-26，剩余股数是180000/初始股数500000=0.36，交易比例为0.02(500000*0.02=10000)，网格间距为0.03元，T操作启动阈值为1.20元
        # 前提是要预留10W元现金，初始现金为10W，初始持仓为50W股，网格交易启动阈值为1.20元，网格间距为0.03元，每次交易股数为10000股
        # 注意前期要一次性买入50W股，后期必须进行清仓操作并中止网格交易．
        
        # [4] 策略对比总结：
        # 网格交易收益率：+70.91%
        # 网格交易最大回撤金额：193,499.98（发生日期：2024-09-23）
        # Buy-and-Hold收益率：+86.19%
        # Buy-and-Hold最大回撤金额：193,499.98（发生日期：2024-09-23）
        # 超额收益：-15.28%
            "grid_start_threshold": 1.20,
            "grid_step": 0.03,
            "trade_ratio": 0.02,
        },
        "aggressive": {
            "grid_start_threshold": 1.25,
            "grid_step": 0.08,
            "trade_ratio": 0.15,
        },
    }
    if mode not in profiles:
        raise ValueError(f"Unsupported mode: {mode}")

    params = profiles[mode].copy()
    params["trade_shares"] = max(1, int(init_shares * params["trade_ratio"]))
    return params


# ─────────────────────────────────────────────
# 2. 网格交易回测
# ─────────────────────────────────────────────
def backtest_grid(
    df: pd.DataFrame,
    init_cash: float = 100_000.0,
    init_shares: int = 500_000,
    base_price: float = 1.3,
    grid_step: float = 0.1,
    trade_shares: int = 20_000,
    pre_sell_price: float = 1.5,
    pre_sell_ratio: float = 0.5,
    pnl_base_total: float | None = None,
    debug: bool = False,
):
    """
    网格规则（基于当日最高/最低价）：
    - 当日最高价 >= BASE + 0.1：卖出，成交价=BASE+0.1
    - 当日最低价 <= BASE - 0.1：买入，成交价=BASE-0.1
    - 每次成交后 BASE 更新为最新成交价
    - 若单日内跨越多个网格，按网格逐档连续成交
    """
    cash = init_cash
    shares = init_shares
    current_base = base_price
    trades = []
    equity_points = []
    if pnl_base_total is None:
        pnl_base_total = init_cash + init_shares * base_price
    
    if debug:
        #print(f"    [DEBUG] 初始BASE={current_base}, 数据范围={df['close'].min():.4f}~{df['close'].max():.4f}")
        #print(f"    [DEBUG] 买入触发条件: 当日最低价 <= {current_base - grid_step:.4f}")
        #print(f"    [DEBUG] 卖出触发条件: 当日最高价 >= {current_base + grid_step:.4f}\n")
        debug = True

    for date, row in df.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        decimals = max(1, len(str(grid_step).split(".")[-1]))

        # 先处理卖出（高价）
        while high >= current_base + grid_step and shares >= trade_shares:
            trade_price = round(current_base + grid_step, decimals)
            shares -= trade_shares
            cash += trade_shares * trade_price
            current_base = trade_price
            if debug:
                print(f"    [DEBUG] {date.date()} 触发卖出: 最高价{high:.4f} >= {trade_price:.4f}, BASE更新为{current_base:.4f}")
            total_assets = cash + shares * close
            profit = total_assets - pnl_base_total
            profit_pct = (profit / pnl_base_total * 100) if pnl_base_total else 0.0
            trades.append(
                {
                    "date": date,
                    "action": "SELL",
                    "price": trade_price,
                    "shares": trade_shares,
                    "position": shares,
                    "cash": cash,
                    "base_price": current_base,
                    "total_assets": total_assets,
                    "profit": profit,
                    "profit_pct": profit_pct,
                }
            )

        # 再处理买入（低价）
        # 再处理买入（低价）
        while low <= current_base - grid_step:
            trade_price = round(current_base - grid_step, decimals)
            cost = trade_shares * trade_price
            if cash < cost:
                if debug:
                    print(
                        f"    [DEBUG] {date.date()} 触发买入条件但未成交: "
                        f"最低价{low:.4f} <= {trade_price:.4f}，现金不足（需{cost:,.0f}，有{cash:,.0f}）"
                    )
                break
            shares += trade_shares
            cash -= cost
            current_base = trade_price
            if debug:
                print(f"    [DEBUG] {date.date()} 触发买入: 最低价{low:.4f} <= {trade_price:.4f}, BASE更新为{current_base:.4f}")
            total_assets = cash + shares * close
            profit = total_assets - pnl_base_total
            profit_pct = (profit / pnl_base_total * 100) if pnl_base_total else 0.0
            trades.append(
                {
                    "date": date,
                    "action": "BUY",
                    "price": trade_price,
                    "shares": trade_shares,
                    "position": shares,
                    "cash": cash,
                    "base_price": current_base,
                    "total_assets": total_assets,
                    "profit": profit,
                    "profit_pct": profit_pct,
                }
            )

        # 记录每个交易日收盘后的策略总资产曲线，用于回撤统计
        equity_points.append({"date": date, "equity": cash + shares * close})

    final_price = float(df["close"].iloc[-1])
    total = cash + shares * final_price
    equity_curve = pd.DataFrame(equity_points).set_index("date")["equity"] if equity_points else pd.Series(dtype=float)

    return total, pd.DataFrame(trades), shares, cash, current_base, equity_curve


# ─────────────────────────────────────────────
# 4. 主流程
# ─────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="Grid trading demo with risk mode switch")
    parser.add_argument(
        "--mode",
        choices=["conservative", "normal", "aggressive"],
        default="normal",
        help="Risk mode for grid parameters",
    )
    return parser.parse_args()


def main(mode: str = "normal"):
    SYMBOL = "588000"     # 科创50ETF华夏；A股: "000001" 平安银行 / 港股: "0700.HK" / 美股: "AAPL"
    START = "20230701"
    END = "20260626"
    INIT_CASH = 100_000.0
    INIT_SHARES = 500_000
    mode_params = get_mode_params(mode, INIT_SHARES)
    GRID_START_THRESHOLD = mode_params["grid_start_threshold"]
    GRID_STEP = mode_params["grid_step"]
    TRADE_SHARES = mode_params["trade_shares"]
    PRE_SELL_PRICE = GRID_START_THRESHOLD
    PRE_SELL_RATIO = 0.5

    print(f"[0] 参数档位: {mode}")
    print(
        f"    启动阈值={GRID_START_THRESHOLD:.2f}, 网格间距={GRID_STEP:.2f}, 单次交易股数={TRADE_SHARES}"
    )
    print(f"[1] 获取 {SYMBOL} 日K数据 {START}~{END} ...")
    df = get_daily_data(SYMBOL, START, END)
    print(f"    共 {len(df)} 个交易日\n")
    print("    数据时间范围：")
    print(f"    从 {df.index[0].date()} 到 {df.index[-1].date()}\n")

    first_open_price = float(df["open"].iloc[0])
    grid_start_df = df[df["high"] > GRID_START_THRESHOLD]

    if grid_start_df.empty:
        print("[2] 网格交易回测...")
        print(f"    条件: 仅当价格 > {GRID_START_THRESHOLD:.2f} 元时启动网格")
        print("    结果: 当前区间内价格从未超过启动阈值，不启动网格交易。\n")

        initial_total = INIT_CASH + INIT_SHARES * first_open_price
        final_price = float(df["close"].iloc[-1])
        bnh_shares, bnh_cash, bnh_curve, bnh_pre_sell_done = backtest_benchmark_with_pre_sell(
            df,
            init_cash=INIT_CASH,
            init_shares=INIT_SHARES,
            pre_sell_price=PRE_SELL_PRICE,
            pre_sell_ratio=PRE_SELL_RATIO,
        )
        final_stock_value = bnh_shares * final_price
        final_total_assets = bnh_cash + final_stock_value
        pct = (final_total_assets - initial_total) / initial_total * 100 if initial_total else 0.0

        print(f"    初始现金：{INIT_CASH:,.2f}")
        print(f"    初始持仓：{INIT_SHARES:,} 股")
        print(f"    首日开盘价：{first_open_price:.4f}")
        print(f"    初始总资产（按首日开盘价估值）：{initial_total:,.2f}")
        print(f"    期末收盘价：{final_price:.4f}")
        print(f"    股票市值：{final_stock_value:,.2f}")
        print(f"    现金资产：{bnh_cash:,.2f}")
        print(f"    总资产（股票市值+现金资产）：{final_total_assets:,.2f}")
        print(f"    期初总资产 = {initial_total:,.2f}")
        print(f"    期末总资产 = {final_total_assets:,.2f}")
        print(f"    收益率 = ({final_total_assets:,.2f} - {initial_total:,.2f}) / {initial_total:,.2f} = {pct:+.2f}%")
        print(
            f"    资产明细：{bnh_shares:,}股 * {final_price:.4f} = {final_stock_value:,.2f} "
            f"+ 现金{bnh_cash:,.2f} = {final_total_assets:,.2f}"
        )
        bnh_mdd, bnh_mdd_date = calc_max_drawdown(bnh_curve)
        pre_sell_msg = "已触发" if bnh_pre_sell_done else "未触发"
        print(f"    基准首次触达减仓：{pre_sell_msg}（阈值={PRE_SELL_PRICE:.2f}，减仓比例={int(PRE_SELL_RATIO * 100)}%）")
        print(f"    总收益率：{pct:+.2f}%")
        bnh_mdd_date_str = bnh_mdd_date.date().isoformat() if bnh_mdd_date is not None else "N/A"
        print(f"    最大回撤金额：{bnh_mdd:,.2f}（发生日期：{bnh_mdd_date_str}）\\n")
        print("[3] 没有产生交易（未达到网格启动阈值）")
        return

    grid_start_date = grid_start_df.index[0]
    grid_df = df.loc[grid_start_date:]
    grid_start_open = float(grid_df["open"].iloc[0])
    BASE_PRICE = round(grid_start_open / GRID_STEP) * GRID_STEP

    print("[2] 网格交易回测...")
    print(
        f"    参数: 初始持仓={INIT_SHARES}股, 启动条件=价格>{GRID_START_THRESHOLD:.2f}元, "
        f"启动日={grid_start_date.date()}, BASE={BASE_PRICE:.1f}（基于启动日开盘价{grid_start_open:.4f}自动计算）, "
        f"每变动{GRID_STEP}元交易{TRADE_SHARES}股"
    )
    initial_total = INIT_CASH + INIT_SHARES * first_open_price
    final_value, trades, final_shares, final_cash, final_base, grid_equity_curve = backtest_grid(
        grid_df,
        init_cash=INIT_CASH,
        init_shares=INIT_SHARES,
        base_price=BASE_PRICE,
        grid_step=GRID_STEP,
        trade_shares=TRADE_SHARES,
        pre_sell_price=PRE_SELL_PRICE,
        pre_sell_ratio=PRE_SELL_RATIO,
        pnl_base_total=initial_total,
        debug=True,
    )
    final_price = float(df["close"].iloc[-1])
    final_stock_value = final_shares * final_price
    final_total_assets = final_cash + final_stock_value
    total_profit_amount = final_total_assets - initial_total
    realized_pnl, unrealized_pnl, decomposed_total_pnl, avg_cost_end = calc_realized_unrealized_pnl(
        trades,
        initial_shares=INIT_SHARES,
        initial_cost_per_share=first_open_price,
        final_price=final_price,
    )

    pre_grid_curve = INIT_CASH + INIT_SHARES * df[df.index < grid_start_date]["close"]
    strategy_curve = pd.concat([pre_grid_curve, grid_equity_curve])
    strategy_mdd, strategy_mdd_date = calc_max_drawdown(strategy_curve)
    bnh_shares, bnh_cash, bnh_curve, bnh_pre_sell_done = backtest_benchmark_with_pre_sell(
        df,
        init_cash=INIT_CASH,
        init_shares=INIT_SHARES,
        pre_sell_price=PRE_SELL_PRICE,
        pre_sell_ratio=PRE_SELL_RATIO,
    )
    bnh_mdd, bnh_mdd_date = calc_max_drawdown(bnh_curve)
    pct = (final_total_assets - initial_total) / initial_total * 100 if initial_total else 0.0

    print(f"    初始现金：{INIT_CASH:,.2f}")
    print(f"    初始持仓：{INIT_SHARES:,} 股")
    print(f"    首日开盘价：{first_open_price:.4f}")
    print(f"    网格启动日：{grid_start_date.date()}")
    print(f"    启动日开盘价：{grid_start_open:.4f}")
    print(f"    自动计算的BASE：{BASE_PRICE:.4f}（按启动日开盘价整0.1元）")
    print(f"    初始总资产（按首日开盘价估值）：{initial_total:,.2f}")
    print(f"    期末收盘价：{final_price:.4f}")
    print(f"    股票市值：{final_stock_value:,.2f}")
    print(f"    现金资产：{final_cash:,.2f}")
    print(f"    总资产（股票市值+现金资产）：{final_total_assets:,.2f}")
    print(f"    期初总资产 = {initial_total:,.2f}")
    print(f"    期末总资产 = {final_total_assets:,.2f}")
    print(f"    收益率 = ({final_total_assets:,.2f} - {initial_total:,.2f}) / {initial_total:,.2f} = {pct:+.2f}%")
    print(
        f"    资产明细：{final_shares:,}股 * {final_price:.4f} = {final_stock_value:,.2f} "
        f"+ 现金{final_cash:,.2f} = {final_total_assets:,.2f}"
    )
    print("    收益分解（平均成本法，配对成交口径）：")
    print(f"      已实现收益：{realized_pnl:+,.2f}")
    print(f"      未实现收益：{unrealized_pnl:+,.2f}")
    print(f"      分解合计：{decomposed_total_pnl:+,.2f}")
    print(f"      总收益额：{total_profit_amount:+,.2f}（= 期末总资产 - 期初总资产）")
    print(f"      期末持仓平均成本：{avg_cost_end:.4f}")
    print(f"      一致性校验（分解合计-总收益额）：{(decomposed_total_pnl - total_profit_amount):+.6f}")
    print(f"    总收益率：{pct:+.2f}%\n")
    
    # 显示买卖触发条件的诊断信息
    buy_trigger = round(BASE_PRICE - GRID_STEP, 4)
    sell_trigger = round(BASE_PRICE + GRID_STEP, 4)
    min_price = df["close"].min()
    max_price = df["close"].max()
    min_date = df["close"].idxmin()
    max_date = df["close"].idxmax()
    print(f"    [诊断] 数据最低价：{min_price:.4f}（{min_date.date()}），最高价：{max_price:.4f}（{max_date.date()}）")
    print(f"    [诊断] 买入触发条件：价格 <= {buy_trigger:.4f}")
    print(f"    [诊断] 卖出触发条件：价格 >= {sell_trigger:.4f}")
    if min_price > buy_trigger:
        print(f"    [诊断] ⚠️ 最低价{min_price:.4f} > 买入条件{buy_trigger:.4f}，不会触发买入")
    if max_price < sell_trigger:
        print(f"    [诊断] ⚠️ 最高价{max_price:.4f} < 卖出条件{sell_trigger:.4f}，不会触发卖出")
    print()
    
    print(f"    最终持仓：{final_shares:,} 股")
    print(f"    最终BASE：{final_base:.4f}\n")

    # 计算 buy-and-hold（不做任何交易）的对比基准
    print("[3] 基准对比（Buy-and-Hold）:")
    bnh_final_stock_value = bnh_shares * final_price
    bnh_total_assets = bnh_cash + bnh_final_stock_value
    bnh_pct = (bnh_total_assets - initial_total) / initial_total * 100 if initial_total else 0.0
    print(f"    最终持仓：{bnh_shares:,} 股（未变动）")
    print(f"    股票市值：{bnh_final_stock_value:,.2f}")
    print(f"    现金资产：{bnh_cash:,.2f}（未变动）")
    print(f"    总资产：{bnh_total_assets:,.2f}")
    print(f"    期初总资产 = {initial_total:,.2f}")
    print(f"    期末总资产 = {bnh_total_assets:,.2f}")
    print(f"    收益率 = ({bnh_total_assets:,.2f} - {initial_total:,.2f}) / {initial_total:,.2f} = {bnh_pct:+.2f}%")
    print(
        f"    资产明细：{bnh_shares:,}股 * {final_price:.4f} = {bnh_final_stock_value:,.2f} "
        f"+ 现金{bnh_cash:,.2f} = {bnh_total_assets:,.2f}"
    )
    print("    首次触达减仓：已关闭")
    print(f"    收益率：{bnh_pct:+.2f}%\n")

    # 输出对比
    print("[4] 策略对比总结：")
    print(f"    网格交易收益率：{pct:+.2f}%")
    strategy_mdd_date_str = strategy_mdd_date.date().isoformat() if strategy_mdd_date is not None else "N/A"
    bnh_mdd_date_str = bnh_mdd_date.date().isoformat() if bnh_mdd_date is not None else "N/A"
    print(f"    网格交易最大回撤金额：{strategy_mdd:,.2f}（发生日期：{strategy_mdd_date_str}）")
    print(f"    Buy-and-Hold收益率：{bnh_pct:+.2f}%")
    print(f"    Buy-and-Hold最大回撤金额：{bnh_mdd:,.2f}（发生日期：{bnh_mdd_date_str}）")
    diff = pct - bnh_pct
    print(f"    超额收益：{diff:+.2f}%\n")

    if not trades.empty:
        print("[5] 交易记录：")
        pd.set_option("display.max_rows", None)
        # 格式化显示交易记录，重点突出盈亏
        display_trades = trades.copy()
        display_trades["date"] = display_trades["date"].dt.strftime("%Y-%m-%d")
        display_trades = display_trades[[
            "date", "action", "price", "shares", "position", "cash", "base_price", "total_assets", "profit", "profit_pct"
        ]]
        # 重新命名列以便显示
        display_trades.columns = [
            "日期", "操作", "成交价", "交易量", "最终持仓", "剩余现金", "更新BASE", "总资产", "盈亏额", "盈亏率%"
        ]
        print(display_trades.to_string(index=False))
        
        # 交易汇总统计
        print("\n[5.1] 交易汇总统计：")
        buy_trades = trades[trades["action"] == "BUY"]
        sell_trades = trades[trades["action"] == "SELL"]
        total_buy_cost = (buy_trades["price"] * buy_trades["shares"]).sum()
        total_sell_revenue = (sell_trades["price"] * sell_trades["shares"]).sum()
        trade_cash_net_inflow = total_sell_revenue - total_buy_cost
        cash_change = final_cash - INIT_CASH
        t_trade_count = min(len(buy_trades), len(sell_trades))
        t_profit_per_trade = TRADE_SHARES * GRID_STEP - 10
        t_operation_profit = t_trade_count * t_profit_per_trade
        trade_ratio = (TRADE_SHARES / INIT_SHARES) if INIT_SHARES else 0.0
        buy_cnt = len(buy_trades)
        sell_cnt = len(sell_trades)
        net_sell_cnt = max(0, sell_cnt - buy_cnt)
        current_progress_price = GRID_START_THRESHOLD + GRID_STEP * net_sell_cnt
        sell_ratio_progress = trade_ratio * net_sell_cnt
        cnt_to_full_sell = int(np.ceil(1.0 / trade_ratio)) if trade_ratio > 0 else 0
        net_sell_shares_at_full = min(INIT_SHARES, cnt_to_full_sell * TRADE_SHARES)
        full_position_target_price = (
            GRID_START_THRESHOLD + GRID_STEP * cnt_to_full_sell if cnt_to_full_sell > 0 else GRID_START_THRESHOLD
        )
        print(f"    买入笔数：{len(buy_trades)}")
        print(f"    卖出笔数：{len(sell_trades)}")
        print(f"    T操作笔数：{t_trade_count}")
        print(f"    每次T操作金额：{t_profit_per_trade:,.2f}")
        print(f"    T操作收益：{t_operation_profit:,.2f}")
        print(
            f"    当前进度价格：{current_progress_price:.4f}"
            f"（GRID_START_THRESHOLD + grid_step*净卖出次数）"
        )
        print(f"    累计卖出比例：{sell_ratio_progress:.2%}（trade_ratio*净卖出次数）")
        print(
            f"    到达100%时刻净卖出数量：{net_sell_shares_at_full:,} 股，"
            f"满仓目标价格：{full_position_target_price:.4f}"
        )
        print(f"    总买入成本：{total_buy_cost:,.2f}")
        print(f"    总卖出收入：{total_sell_revenue:,.2f}")
        print(f"    交易现金净流入：{trade_cash_net_inflow:+,.2f}（总卖出收入-总买入成本）")
        print(f"    现金变动：{cash_change:+,.2f}（期末现金-期初现金）")
    else:
        print("[5] 没有产生交易（价格未触发网格阈值）")


if __name__ == "__main__":
    args = parse_args()
    main(mode=args.mode)
