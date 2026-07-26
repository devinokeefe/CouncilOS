from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

OUT = Path('tmp_ibkr_sizing/output')
OUT.mkdir(parents=True, exist_ok=True)

NAV_GBP = 1_000_000.0
SLEEVE_VOL = 0.10
OUTER_SCALE = 3.094384607276956
OUTER_CAP = 15.0
WEEKS = 52.0

WEIGHTS = pd.Series({
    'Growth_QQQ': 0.3777777777777778,
    'Trend': 0.23611111111111108,
    'Rates_US5': 0.11333333333333331,
    'Styles': 0.15,
    'Gold': 0.03777777777777777,
    'Corn': 0.018888888888888886,
    'Silver': 0.018888888888888886,
    'Bitcoin': 0.04722222222222222,
}, dtype=float)

# Orderable research proxies. QSPNX and DBMF are not exact replications of the
# academic styles sleeve or the 38-market TSMOM sleeve.
TICKERS = {
    'Growth_QQQ': 'QQQ',
    'Trend': 'DBMF',
    'Rates_US5': 'ZF=F',
    'Styles': 'QSPNX',
    'Gold': 'GC=F',
    'Corn': 'ZC=F',
    'Silver': 'SI=F',
    'Bitcoin': 'BTC-USD',
    'NQ_FUT': 'NQ=F',
    'GBPUSD': 'GBPUSD=X',
}


def download_close(ticker: str) -> pd.Series:
    df = yf.download(
        ticker,
        start='2012-01-01',
        end='2026-07-27',
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f'No data returned for {ticker}')
    if isinstance(df.columns, pd.MultiIndex):
        if ('Close', ticker) in df.columns:
            s = df[('Close', ticker)]
        else:
            s = df.xs('Close', level=0, axis=1).iloc[:, 0]
    else:
        s = df['Close']
    s = pd.to_numeric(s, errors='coerce').dropna().sort_index()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s.name = ticker
    return s[~s.index.duplicated(keep='last')]


def robust_weekly_vol(s: pd.Series, fast: float, slow: float, floor: float = 0.02) -> pd.Series:
    fastv = s.ewm(halflife=fast, adjust=False, min_periods=max(4, int(fast))).std(bias=False).shift(1) * math.sqrt(WEEKS)
    slowv = s.ewm(halflife=slow, adjust=False, min_periods=max(8, int(slow))).std(bias=False).shift(1) * math.sqrt(WEEKS)
    expv = s.expanding(min_periods=52).std(ddof=1).shift(1) * math.sqrt(WEEKS)
    forecast = pd.concat([fastv, 0.8 * slowv, 0.55 * expv], axis=1).max(axis=1)
    fallback = s.expanding(min_periods=8).std(ddof=1).shift(1) * math.sqrt(WEEKS)
    return forecast.fillna(fallback).fillna(0.10).clip(lower=floor)


prices: dict[str, pd.Series] = {}
for name, ticker in TICKERS.items():
    try:
        prices[name] = download_close(ticker)
    except Exception as exc:
        if name == 'Styles':
            # Alternate share classes if the N-class NAV is unavailable.
            for alt in ['QSPRX', 'QSPIX']:
                try:
                    prices[name] = download_close(alt)
                    TICKERS[name] = alt
                    break
                except Exception:
                    pass
        if name not in prices:
            raise RuntimeError(f'Failed to download {name}/{ticker}: {exc}') from exc

# Common US weekday calendar. Daily returns are resampled to Friday weekly sums,
# matching the study engine's sleeve-risk estimation convention.
raw_weekly: dict[str, pd.Series] = {}
normalized_weekly: dict[str, pd.Series] = {}
sleeve_forecast: dict[str, float] = {}
sleeve_scale: dict[str, float] = {}

for sleeve in WEIGHTS.index:
    daily = prices[sleeve].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    weekly = daily.resample('W-FRI').sum(min_count=1)
    vol = robust_weekly_vol(weekly, fast=13, slow=52, floor=0.02)
    current_vol = float(vol.dropna().iloc[-1])
    current_scale = SLEEVE_VOL / current_vol
    raw_weekly[sleeve] = weekly
    normalized_weekly[sleeve] = weekly * (SLEEVE_VOL / vol)
    sleeve_forecast[sleeve] = current_vol
    sleeve_scale[sleeve] = current_scale

norm_df = pd.concat(normalized_weekly, axis=1).sort_index()
# Reallocate weights proportionally when a proxy was not yet available.
weighted = norm_df.mul(WEIGHTS, axis=1)
available_weight = norm_df.notna().mul(WEIGHTS, axis=1).sum(axis=1)
combined = weighted.sum(axis=1, min_count=1).div(available_weight.where(available_weight > 0))
combined = combined.dropna()
outer_vol_series = robust_weekly_vol(combined, fast=4, slow=26, floor=0.02)
outer_forecast = float(outer_vol_series.dropna().iloc[-1])
reference_leverage = SLEEVE_VOL / outer_forecast
total_leverage = min(OUTER_CAP, reference_leverage * OUTER_SCALE)

latest_date = min(s.index[-1] for s in prices.values())
latest = {name: float(s.asof(latest_date)) for name, s in prices.items()}
gbpusd = latest['GBPUSD']
nav_usd = NAV_GBP * gbpusd

exposure_ratio = {
    sleeve: total_leverage * float(WEIGHTS[sleeve]) * float(sleeve_scale[sleeve])
    for sleeve in WEIGHTS.index
}
target_usd = {sleeve: nav_usd * exposure_ratio[sleeve] for sleeve in WEIGHTS.index}

# IBKR proxy instruments and continuous-price contract notionals.
instruments = {
    'Growth_QQQ': {'symbol': 'MNQ', 'expiry': '202609', 'exchange': 'CME', 'currency': 'USD', 'type': 'FUT', 'price': latest['NQ_FUT'], 'contract_notional': latest['NQ_FUT'] * 2.0, 'note': '$2 × Nasdaq-100 index'},
    'Trend': {'symbol': 'DBMF', 'expiry': '', 'exchange': 'ARCA', 'currency': 'USD', 'type': 'STK', 'price': latest['Trend'], 'contract_notional': latest['Trend'], 'note': 'Managed-futures ETF proxy; not the exact 38-market TSMOM book'},
    'Rates_US5': {'symbol': 'ZF', 'expiry': '202609', 'exchange': 'CBOT', 'currency': 'USD', 'type': 'FUT', 'price': latest['Rates_US5'], 'contract_notional': latest['Rates_US5'] * 1000.0, 'note': '$1,000 per index point'},
    'Styles': {'symbol': TICKERS['Styles'], 'expiry': '', 'exchange': 'FUNDSERV', 'currency': 'USD', 'type': 'FUND', 'price': latest['Styles'], 'contract_notional': latest['Styles'], 'note': 'AQR Style Premia Alternative proxy; not exact HML/RMW/CMA/Mom'},
    'Gold': {'symbol': 'MGC', 'expiry': '202612', 'exchange': 'COMEX', 'currency': 'USD', 'type': 'FUT', 'price': latest['Gold'], 'contract_notional': latest['Gold'] * 10.0, 'note': '10 troy ounces'},
    'Corn': {'symbol': 'MZC', 'expiry': '202612', 'exchange': 'CBOT', 'currency': 'USD', 'type': 'FUT', 'price': latest['Corn'], 'contract_notional': latest['Corn'] * 5.0, 'note': '500 bushels; quote is cents/bushel'},
    'Silver': {'symbol': 'SIL', 'expiry': '202609', 'exchange': 'COMEX', 'currency': 'USD', 'type': 'FUT', 'price': latest['Silver'], 'contract_notional': latest['Silver'] * 1000.0, 'note': '1,000 troy ounces'},
    'Bitcoin': {'symbol': 'MBT', 'expiry': '202609', 'exchange': 'CME', 'currency': 'USD', 'type': 'FUT', 'price': latest['Bitcoin'], 'contract_notional': latest['Bitcoin'] * 0.1, 'note': '0.1 bitcoin'},
}

order_rows = []
for sleeve, inst in instruments.items():
    ideal_units = target_usd[sleeve] / inst['contract_notional']
    if inst['type'] == 'FUT':
        order_units = int(round(ideal_units))
    elif inst['type'] == 'STK':
        order_units = int(round(ideal_units))
    else:
        order_units = round(ideal_units, 3)
    achieved = order_units * inst['contract_notional']
    order_rows.append({
        'Sleeve': sleeve,
        'Risk_budget': float(WEIGHTS[sleeve]),
        'Forecast_vol': sleeve_forecast[sleeve],
        'Sleeve_scale_to_10pct': sleeve_scale[sleeve],
        'Total_outer_leverage': total_leverage,
        'Target_exposure_ratio_NAV': exposure_ratio[sleeve],
        'Target_USD_notional_per_GBP1m': target_usd[sleeve],
        'Instrument': inst['symbol'],
        'Security_type': inst['type'],
        'Exchange': inst['exchange'],
        'Expiry_YYYYMM': inst['expiry'],
        'Reference_price': inst['price'],
        'USD_notional_per_unit': inst['contract_notional'],
        'Ideal_units': ideal_units,
        'Rounded_order_units': order_units,
        'Achieved_USD_notional': achieved,
        'Rounding_error_USD': achieved - target_usd[sleeve],
        'Action_from_cash': 'BUY' if order_units >= 0 else 'SELL',
        'Notes': inst['note'],
    })

orders = pd.DataFrame(order_rows)
orders.to_csv(OUT / 'ibkr_proxy_orders_per_gbp1m.csv', index=False)

market_rows = []
for name, ticker in TICKERS.items():
    market_rows.append({'Series': name, 'Ticker': ticker, 'Last_date': str(prices[name].index[-1].date()), 'Reference_date': str(latest_date.date()), 'Reference_price': latest[name]})
pd.DataFrame(market_rows).to_csv(OUT / 'market_data.csv', index=False)

pd.DataFrame({
    'Risk_budget': WEIGHTS,
    'Forecast_vol': pd.Series(sleeve_forecast),
    'Sleeve_scale_to_10pct': pd.Series(sleeve_scale),
    'Exposure_ratio_NAV': pd.Series(exposure_ratio),
    'Target_USD_notional_per_GBP1m': pd.Series(target_usd),
}).to_csv(OUT / 'current_target_exposures.csv')

summary = {
    'reference_date': str(latest_date.date()),
    'nav_gbp_assumption': NAV_GBP,
    'gbpusd': gbpusd,
    'nav_usd': nav_usd,
    'outer_forecast_vol': outer_forecast,
    'reference_leverage_10pct_over_forecast': reference_leverage,
    'historical_outer_scale': OUTER_SCALE,
    'total_live_leverage': total_leverage,
    'gross_target_exposure_ratio': float(sum(abs(x) for x in exposure_ratio.values())),
    'weights': WEIGHTS.to_dict(),
    'sleeve_forecast_vol': sleeve_forecast,
    'sleeve_scale_to_10pct': sleeve_scale,
    'target_exposure_ratio_nav': exposure_ratio,
    'proxy_warning': 'DBMF and QSPNX are orderable proxies, not exact replications of the research trend and academic style sleeves. Futures quantities are rounded to whole contracts.',
}
(OUT / 'sizing_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

print(json.dumps(summary, indent=2))
print(orders.to_string(index=False))
