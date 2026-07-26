from __future__ import annotations

import io
import json
import math
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path('/mnt/data')
OUT = ROOT / 'daily_results'
EXP = ROOT / 'portfolio_experiments'
GOB = ROOT / 'gobundle'
SRC = OUT / 'source_data'
for p in [OUT, EXP, GOB, SRC]:
    p.mkdir(parents=True, exist_ok=True)

START = pd.Timestamp('1999-01-01')
END = pd.Timestamp('2023-10-07')
EVAL_START = pd.Timestamp('2005-01-07')
EVAL_END = pd.Timestamp('2023-10-06')
TARGET_VOL = 0.325
SLEEVE_VOL = 0.10
TRADING_DAYS = 252.0
WEEKS = 52.0
BASE_ANNUAL_COST = 0.01 * TARGET_VOL / 0.35
RNG = np.random.default_rng(20260726)

ASSETS = [
    'Growth_QQQ', 'Broad_US', 'Trend', 'Rates_US5', 'Styles',
    'Gold', 'Corn', 'Silver', 'Bitcoin'
]
BASE_WEIGHTS = pd.Series({
    'Growth_QQQ': 0.24,
    'Broad_US': 0.16,
    'Trend': 0.25,
    'Rates_US5': 0.12,
    'Styles': 0.10,
    'Gold': 0.04,
    'Corn': 0.02,
    'Silver': 0.02,
    'Bitcoin': 0.05,
}, dtype=float)

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 daily-portfolio-research/1.0'})
SOURCE_MANIFEST: list[dict] = []


def log(msg: str):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def fetch_bytes(url: str, *, timeout: int = 60, tries: int = 4) -> bytes:
    err = None
    for k in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as exc:
            err = exc
            time.sleep(2 ** k)
    raise RuntimeError(f'failed to fetch {url}: {err}')


def record_source(name: str, url: str, status: str, detail: str = ''):
    SOURCE_MANIFEST.append({'Source': name, 'URL': url, 'Status': status, 'Detail': detail})


def load_french_zip(url: str, expected_cols: list[str], name: str) -> pd.DataFrame:
    raw = fetch_bytes(url)
    z = zipfile.ZipFile(io.BytesIO(raw))
    member = z.namelist()[0]
    text = z.read(member).decode('latin1')
    record_source(name, url, 'downloaded', member)
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r'^\s*,?(Mkt-RF|Mom)', line):
            start = i
            break
    if start is None:
        raise ValueError(f'Could not find data header in {name}')
    data_lines = [lines[start]]
    for line in lines[start + 1:]:
        first = line.split(',')[0].strip()
        if not re.fullmatch(r'\d{8}', first):
            break
        data_lines.append(line)
    df = pd.read_csv(io.StringIO('\n'.join(data_lines)))
    df.columns = [str(c).strip() or 'Date' for c in df.columns]
    if 'Date' not in df.columns:
        df = df.rename(columns={df.columns[0]: 'Date'})
    df['Date'] = pd.to_datetime(df['Date'].astype(str), format='%Y%m%d')
    df = df.set_index('Date').sort_index()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors='coerce') / 100.0
    missing = [c for c in expected_cols if c not in df.columns]
    if missing:
        raise ValueError(f'{name} missing expected columns {missing}; got {list(df.columns)}')
    return df


def download_yahoo(ticker: str, name: str) -> pd.Series:
    import yfinance as yf
    err = None
    for auto_adjust in [False, True]:
        try:
            df = yf.download(ticker, start=START.strftime('%Y-%m-%d'), end=END.strftime('%Y-%m-%d'),
                             auto_adjust=auto_adjust, progress=False, threads=False)
            if df.empty:
                raise ValueError('empty Yahoo response')
            if isinstance(df.columns, pd.MultiIndex):
                if ('Close', ticker) in df.columns:
                    s = df[('Close', ticker)]
                else:
                    s = df.xs('Close', level=0, axis=1).iloc[:, 0]
            else:
                s = df['Close']
            s = pd.to_numeric(s, errors='coerce').dropna().sort_index()
            s.index = pd.to_datetime(s.index).tz_localize(None)
            s.name = name
            record_source(name, f'https://query1.finance.yahoo.com/ ({ticker})', 'downloaded',
                          f'{len(s)} closes; yfinance auto_adjust={auto_adjust}')
            return s
        except Exception as exc:
            err = exc
    try:
        p1, p2 = int(START.timestamp()), int(END.timestamp())
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}&interval=1d&events=history'
        payload = json.loads(fetch_bytes(url).decode('utf-8'))
        result = payload['chart']['result'][0]
        idx = pd.to_datetime(result['timestamp'], unit='s', utc=True).tz_convert(None).normalize()
        vals = result['indicators']['quote'][0]['close']
        s = pd.Series(vals, index=idx, name=name, dtype=float).dropna().sort_index()
        record_source(name, url, 'downloaded', f'{len(s)} closes; chart API fallback')
        return s
    except Exception as exc:
        err = exc
    record_source(name, f'https://query1.finance.yahoo.com/ ({ticker})', 'failed', repr(err))
    raise RuntimeError(f'Yahoo download failed for {ticker}: {err}')


def robust_weekly_vol(s: pd.Series, floor: float = 0.02, fast: float = 13, slow: float = 52) -> pd.Series:
    fastv = s.ewm(halflife=fast, adjust=False, min_periods=13).std(bias=False).shift(1) * math.sqrt(WEEKS)
    slowv = s.ewm(halflife=slow, adjust=False, min_periods=52).std(bias=False).shift(1) * math.sqrt(WEEKS)
    expv = s.expanding(min_periods=52).std(ddof=1).shift(1) * math.sqrt(WEEKS)
    forecast = pd.concat([fastv, 0.8 * slowv, 0.55 * expv], axis=1).max(axis=1)
    fallback = s.expanding(min_periods=8).std(ddof=1).shift(1) * math.sqrt(WEEKS)
    return forecast.fillna(fallback).fillna(0.05).clip(lower=floor)


def week_label(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.to_period('W-FRI').end_time.normalize()


def weekly_scale_daily(raw_daily: pd.Series, target: float = SLEEVE_VOL) -> tuple[pd.Series, pd.Series, pd.Series]:
    raw_daily = raw_daily.sort_index()
    weekly = raw_daily.resample('W-FRI').sum(min_count=1)
    forecast = robust_weekly_vol(weekly)
    scale = target / forecast
    labels = pd.Series(week_label(raw_daily.index), index=raw_daily.index)
    daily_scale = labels.map(scale).astype(float)
    scaled = raw_daily * daily_scale.values
    return scaled, scale, forecast


def parse_adjusted_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    valuecol = 'price' if 'price' in df.columns else df.columns[-1]
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df[valuecol], errors='coerce').values, index=idx, name=path.stem)
    return s[~s.index.duplicated(keep='last')].sort_index().dropna()


def parse_multiple_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    if 'PRICE' not in df.columns:
        raise ValueError(f'{path} lacks PRICE column')
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df['PRICE'], errors='coerce').values, index=idx, name=path.stem)
    return s[~s.index.duplicated(keep='last')].sort_index().dropna()


def futures_raw_return(repo: Path, instrument: str) -> pd.Series:
    ap = repo / 'data/futures/adjusted_prices_csv' / f'{instrument}.csv'
    mp = repo / 'data/futures/multiple_prices_csv' / f'{instrument}.csv'
    if not ap.exists() or not mp.exists():
        raise FileNotFoundError(instrument)
    adjusted = parse_adjusted_csv(ap)
    current = parse_multiple_csv(mp)
    df = pd.concat([adjusted.rename('adj'), current.rename('px')], axis=1).sort_index()
    r = df['adj'].diff() / df['px'].shift(1).abs()
    r = r.replace([np.inf, -np.inf], np.nan)
    r.name = instrument
    return r


def clone_pysystemtrade() -> Path:
    dest = ROOT / 'pysystemtrade'
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(['git', 'clone', '--depth', '1', '--branch', 'develop',
                    'https://github.com/pst-group/pysystemtrade.git', str(dest)], check=True)
    record_source('pysystemtrade daily futures data', 'https://github.com/pst-group/pysystemtrade',
                  'cloned', 'develop branch')
    return dest


def choose_trend_instruments(repo: Path) -> tuple[dict[str, list[str]], pd.DataFrame]:
    config = pd.read_csv(repo / 'data/futures/csvconfig/instrumentconfig.csv')
    config = config.drop_duplicates('Instrument').set_index('Instrument')
    preferred = {
        'Equity': ['SP500','NASDAQ','DOW','RUSSELL','FTSE100','DAX','EUROSTX','CAC','AEX','NIKKEI','TOPIX','ASX','KOSPI','HANG','MSCIWORLD'],
        'Rates': ['US2','US3','US5','US10','US20','US30','BOBL','BUND','BUXL','SCHATZ','GILT','JGB','AUS10','CAD10','OAT'],
        'FX': ['EUR','GBP','JPY','CHF','AUD','CAD','NZD','MXP','KRWUSD','PLN','SEK','NOK'],
        'Commodities': ['GOLD','SILVER','COPPER','ALUMINIUM','ZINC_LME','NICKEL_LME','CRUDE_W','BRENT_W','GAS_US','NATGAS','CORN','WHEAT','SOYBEAN','SOYMEAL','SUGAR','COFFEE','COCOA','COTTON','LIVECOW','LEANHOG'],
    }
    selected: dict[str, list[str]] = {}
    rows = []
    for group, wanted in preferred.items():
        valid = []
        for inst in wanted:
            ap = repo / 'data/futures/adjusted_prices_csv' / f'{inst}.csv'
            mp = repo / 'data/futures/multiple_prices_csv' / f'{inst}.csv'
            status, first, last, n = 'missing', pd.NaT, pd.NaT, 0
            asset_class = config.loc[inst, 'AssetClass'] if inst in config.index else ''
            if ap.exists() and mp.exists():
                try:
                    s = futures_raw_return(repo, inst).dropna()
                    first, last, n = s.index.min(), s.index.max(), len(s)
                    if n >= 1500 and first <= pd.Timestamp('2007-01-01') and last >= pd.Timestamp('2023-09-01'):
                        valid.append(inst); status = 'included'
                    else:
                        status = 'insufficient coverage'
                except Exception as exc:
                    status = f'parse failed: {exc}'
            rows.append({'Group': group, 'Instrument': inst, 'AssetClass': asset_class, 'Status': status,
                         'First date': first, 'Last date': last, 'Observations': n})
        selected[group] = valid
    return selected, pd.DataFrame(rows)


def build_trend_raw(repo: Path, master: pd.DatetimeIndex) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    selected, audit = choose_trend_instruments(repo)
    instrument_returns = {}
    for group, instruments in selected.items():
        log(f'Trend group {group}: {len(instruments)} instruments')
        for inst in instruments:
            try:
                instrument_returns[inst] = futures_raw_return(repo, inst).reindex(master).fillna(0.0)
            except Exception as exc:
                audit.loc[audit['Instrument'] == inst, 'Status'] = f'failed in build: {exc}'
    R = pd.DataFrame(instrument_returns, index=master)
    if R.empty:
        raise RuntimeError('No trend instruments could be built')
    fast = R.ewm(halflife=20, adjust=False, min_periods=20).std(bias=False).shift(1) * math.sqrt(TRADING_DAYS)
    slow = R.ewm(halflife=100, adjust=False, min_periods=100).std(bias=False).shift(1) * math.sqrt(TRADING_DAYS)
    expv = R.expanding(min_periods=60).std(ddof=1).shift(1) * math.sqrt(TRADING_DAYS)
    vol = pd.concat({'fast': fast, 'slow': 0.8*slow, 'exp': 0.55*expv}, axis=1)
    vol = pd.DataFrame({c: vol.xs(c, level=1, axis=1).max(axis=1) for c in R.columns}, index=R.index)
    fallback = R.expanding(min_periods=20).std(ddof=1).shift(1) * math.sqrt(TRADING_DAYS)
    vol = vol.fillna(fallback).fillna(0.15).clip(lower=0.04)
    scaled = R * (SLEEVE_VOL / vol)
    signals = []
    for h in [21, 63, 126, 252]:
        signals.append(np.sign(R.rolling(h, min_periods=max(10, h//2)).sum()).shift(1))
    signal = sum(signals) / len(signals)
    pnl = scaled * signal
    group_pnls = {}
    for group, instruments in selected.items():
        use = [i for i in instruments if i in pnl.columns]
        if use:
            group_pnls[group] = pnl[use].mean(axis=1)
    G = pd.DataFrame(group_pnls, index=master)
    trend_raw = G.mean(axis=1)
    trend_raw.name = 'Trend_raw'
    return trend_raw, audit, R


def newey_west_t(diff: pd.Series, lags: int) -> float:
    x = diff.dropna().to_numpy(float)
    n = len(x)
    if n < 3:
        return np.nan
    u = x - x.mean()
    gamma0 = float(np.dot(u, u) / n)
    omega = gamma0
    for lag in range(1, min(lags, n-1) + 1):
        gamma = float(np.dot(u[lag:], u[:-lag]) / n)
        omega += 2.0 * (1.0 - lag/(lags+1.0)) * gamma
    if omega <= 0:
        return np.nan
    return float(x.mean() / math.sqrt(omega / n))


def requested_t(diff: pd.Series) -> float:
    x = diff.dropna().to_numpy(float)
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(len(x))) if sd > 0 else np.nan


def perf(r: pd.Series, rf: pd.Series) -> dict:
    df = pd.concat([r.rename('r'), rf.rename('rf')], axis=1).dropna()
    r, rf = df.r, df.rf
    n = len(r); years = n/TRADING_DAYS
    wealth = (1+r.clip(lower=-.999999)).cumprod(); dd = wealth/wealth.cummax()-1
    ex = r-rf
    return {'Observations': n, 'Years': years,
            'CAGR': float(wealth.iloc[-1]**(1/years)-1),
            'Log growth': float(np.log1p(r.clip(lower=-.999999)).mean()*TRADING_DAYS),
            'Volatility': float(r.std(ddof=1)*math.sqrt(TRADING_DAYS)),
            'Sharpe': float(ex.mean()/ex.std(ddof=1)*math.sqrt(TRADING_DAYS)),
            'Max drawdown': float(dd.min()), 'Worst day': float(r.min()), 'Skew': float(r.skew())}


def equal_vol_daily(r: pd.Series, rf: pd.Series, target: float = TARGET_VOL) -> tuple[pd.Series, float]:
    ex = (r-rf).loc[EVAL_START:EVAL_END].dropna()
    v = ex.std(ddof=1)*math.sqrt(TRADING_DAYS)
    k = target/v if v > 0 else 1.0
    out = rf.reindex(r.index).fillna(0.0) + k*(r-rf.reindex(r.index).fillna(0.0))
    return out, float(k)


def bridge_daily_to_weekly(shape: pd.Series, target: pd.Series, master: pd.DatetimeIndex) -> pd.Series:
    shape = shape.reindex(master).fillna(0.0)
    labels = pd.Series(week_label(master), index=master)
    out = pd.Series(0.0, index=master)
    for wk, idx in labels.groupby(labels).groups.items():
        if wk not in target.index or pd.isna(target.loc[wk]):
            out.loc[idx] = np.nan; continue
        tgt = float(target.loc[wk]); vals = shape.loc[idx]; total = vals.sum()
        if abs(total) > 1e-10:
            out.loc[idx] = vals * (tgt/total)
        else:
            out.loc[idx] = 0.0; out.loc[idx[-1]] = tgt
    return out


def build_daily_from_result(res, source_R: pd.DataFrame, daily_sleeves: pd.DataFrame,
                            weekly_sleeves: pd.DataFrame, rf_daily: pd.Series) -> tuple[pd.Series, str, dict]:
    master = daily_sleeves.index; labels = pd.Series(week_label(master), index=master)
    source_R = source_R.reindex(res.weights.index)
    daily_components, direct_cols, bridged_cols = {}, [], []
    generic_shape = daily_sleeves.drop(columns=['Trend'], errors='ignore').fillna(0.0).mean(axis=1)
    for col in res.weights.columns:
        target_week = source_R[col] if col in source_R.columns else pd.Series(index=res.weights.index, dtype=float)
        if col in daily_sleeves.columns and col in weekly_sleeves.columns:
            base_week = weekly_sleeves[col].reindex(target_week.index)
            paired = pd.concat([base_week.rename('b'), target_week.rename('t')], axis=1).dropna()
            is_direct = len(paired) > 100 and np.nanmax(np.abs(paired.t-paired.b)) < 1e-8
            shape = daily_sleeves[col]
        else:
            is_direct = False; shape = generic_shape
        if is_direct:
            daily_components[col] = shape; direct_cols.append(col)
        else:
            daily_components[col] = bridge_daily_to_weekly(shape, target_week, master); bridged_cols.append(col)
    D = pd.DataFrame(daily_components, index=master)
    w = res.weights.reindex(labels.values); w.index = master
    lev = res.leverage.reindex(labels.values); lev.index = master
    gross_excess = (w*D.fillna(0.0)).sum(axis=1)*lev.fillna(0.0)
    ret = rf_daily.reindex(master).fillna(0.0) + gross_excess - BASE_ANNUAL_COST/TRADING_DAYS
    inc = res.incremental_cost.reindex(labels.values); inc.index = master
    first = ~labels.duplicated(); ret.loc[first] -= inc.loc[first].fillna(0.0)
    status = 'Direct daily source P&L' if not bridged_cols else 'Hybrid daily source P&L; derived/modified weekly sleeves bridged'
    return ret, status, {'Direct columns': ' | '.join(direct_cols), 'Bridged columns': ' | '.join(bridged_cols)}


def max_t_block_pvalues(diff_matrix: pd.DataFrame, observed_t: pd.Series,
                        block: int = 20, paths: int = 750) -> tuple[pd.Series, np.ndarray]:
    X = diff_matrix.to_numpy(float)
    X = X - np.nanmean(X, axis=0, keepdims=True)
    X = np.nan_to_num(X, nan=0.0)
    n, m = X.shape; nblocks = math.ceil(n/block); n_eff = nblocks*block
    cs = np.vstack([np.zeros((1,m)), np.cumsum(X, axis=0)])
    cs2 = np.vstack([np.zeros((1,m)), np.cumsum(X*X, axis=0)])
    block_sum = cs[block:] - cs[:-block]; block_sumsq = cs2[block:] - cs2[:-block]
    starts = np.arange(len(block_sum)); maxima = np.empty(paths)
    done = 0
    while done < paths:
        b = min(25, paths-done)
        picks = RNG.choice(starts, size=(b,nblocks), replace=True)
        sums = block_sum[picks].sum(axis=1); sumsq = block_sumsq[picks].sum(axis=1)
        means = sums/n_eff
        variances = np.maximum((sumsq - sums*sums/n_eff)/(n_eff-1), 0.0)
        sds = np.sqrt(variances)
        ts = np.divide(means, sds, out=np.zeros_like(means), where=sds>0)*math.sqrt(n_eff)
        maxima[done:done+b] = np.nanmax(ts, axis=1); done += b
    pvals = pd.Series({c: (1+np.sum(maxima >= observed_t[c]))/(paths+1) for c in diff_matrix.columns})
    return pvals, maxima


def fetch_cboe_benchmark(code: str) -> pd.Series | None:
    urls = [f'https://cdn.cboe.com/api/global/us_indices/daily_prices/{code}_History.csv',
            f'https://cdn.cboe.com/api/global/us_indices/daily_prices/{code.lower()}_History.csv']
    last = None
    for url in urls:
        try:
            raw = fetch_bytes(url); df = pd.read_csv(io.BytesIO(raw))
            datecol = next(c for c in df.columns if 'DATE' in c.upper())
            candidates = [c for c in df.columns if c != datecol and ('CLOSE' in c.upper() or code.upper() in c.upper())]
            valuecol = candidates[-1] if candidates else df.columns[-1]
            idx = pd.to_datetime(df[datecol], errors='coerce')
            s = pd.Series(pd.to_numeric(df[valuecol], errors='coerce').values, index=idx).dropna().sort_index()
            if len(s) > 1000:
                record_source(f'Cboe {code}', url, 'downloaded', f'{len(s)} observations; column {valuecol}')
                return s
        except Exception as exc:
            last = exc
    record_source(f'Cboe {code}', urls[0], 'failed', repr(last)); return None


def main():
    log('Downloading official Fama-French daily factors')
    ff5 = load_french_zip('https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip',
                          ['Mkt-RF','HML','RMW','CMA','RF'], 'Fama-French 5 factors daily')
    mom = load_french_zip('https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip',
                          ['Mom'], 'Fama-French momentum daily')
    ff = ff5.join(mom[['Mom']], how='inner').sort_index()
    master = ff.loc[START:EVAL_END].index
    ff.to_csv(SRC/'fama_french_daily.csv')

    log('Downloading QQQ and Bitcoin daily closes')
    qqq_px = download_yahoo('QQQ', 'QQQ price close')
    btc_px = download_yahoo('BTC-USD', 'Bitcoin USD close')
    qqq_px.to_csv(SRC/'qqq_close.csv', header=True); btc_px.to_csv(SRC/'bitcoin_close.csv', header=True)

    log('Cloning daily futures source data')
    repo = clone_pysystemtrade()
    qqq_raw = qqq_px.reindex(master).ffill().pct_change()
    broad_raw = ff['Mkt-RF'].reindex(master)
    style_raw = ff[['HML','RMW','CMA','Mom']].mean(axis=1).reindex(master)
    rf_daily = ff['RF'].reindex(master).fillna(0.0)

    futures_map = {'Rates_US5':'US5', 'Gold':'GOLD', 'Corn':'CORN', 'Silver':'SILVER'}
    futures_raw = {}
    for sleeve, inst in futures_map.items():
        log(f'Building {sleeve} from {inst}')
        futures_raw[sleeve] = futures_raw_return(repo, inst).reindex(master).fillna(0.0)
        record_source(sleeve, f'https://github.com/pst-group/pysystemtrade/tree/develop/data/futures', 'downloaded', inst)

    log('Building contract-level cross-asset trend sleeve')
    trend_raw, trend_audit, trend_market_returns = build_trend_raw(repo, master)
    trend_audit.to_csv(OUT/'trend_instrument_audit.csv', index=False)
    trend_market_returns.to_csv(SRC/'trend_market_raw_daily_returns.csv')
    btc_mark = btc_px.reindex(master, method='ffill'); btc_raw = btc_mark.pct_change()
    btc_raw.loc[btc_raw.index < pd.Timestamp('2017-01-03')] = np.nan
    raw = pd.DataFrame({'Growth_QQQ': qqq_raw, 'Broad_US': broad_raw, 'Trend': trend_raw,
                        'Rates_US5': futures_raw['Rates_US5'], 'Styles': style_raw,
                        'Gold': futures_raw['Gold'], 'Corn': futures_raw['Corn'],
                        'Silver': futures_raw['Silver'], 'Bitcoin': btc_raw}, index=master)
    raw.to_csv(OUT/'daily_raw_sleeve_returns.csv')

    log('Normalizing daily P&Ls with weekly lagged risk estimates')
    daily_sleeves = pd.DataFrame(index=master); scale_rows = []
    for col in ASSETS:
        scaled, scale, forecast = weekly_scale_daily(raw[col]); daily_sleeves[col] = scaled
        for dt, v in scale.items():
            scale_rows.append({'Week': dt, 'Sleeve': col, 'Scale': v, 'Forecast vol': forecast.loc[dt]})
    daily_sleeves.to_csv(OUT/'daily_normalized_sleeve_pnls.csv')
    pd.DataFrame(scale_rows).to_csv(OUT/'weekly_sleeve_scaling.csv', index=False)
    weekly_sleeves = daily_sleeves.resample('W-FRI').sum(min_count=1).loc[:EVAL_END]
    rf_weekly = rf_daily.resample('W-FRI').sum(min_count=1).reindex(weekly_sleeves.index).fillna(0.0)

    qqq_raw_master = qqq_raw.reindex(master).fillna(0.0)
    qqq3_daily = 3.0*qqq_raw_master - (0.0082/TRADING_DAYS)
    qqq3_weekly = (1.0+qqq3_daily).resample('W-FRI').prod()-1.0
    qqq_weekly_price = (1.0+qqq_raw_master).resample('W-FRI').prod()-1.0
    cash_weekly = (1.0+rf_daily).resample('W-FRI').prod()-1.0
    qqq55_weekly = 0.55*qqq3_weekly + 0.45*cash_weekly
    inputs = weekly_sleeves.copy(); inputs['RF'] = rf_weekly; inputs.index.name = 'Date'
    inputs.to_csv(GOB/'growth_optimal_weekly_inputs.csv')
    saved = pd.DataFrame({'QQQ 1x (price)': qqq_weekly_price,
                          'QQQ 3x daily synthetic': qqq3_weekly,
                          '55% 3x QQQ + 45% cash': qqq55_weekly}).reindex(weekly_sleeves.index)
    saved.index.name = 'Date'; saved.to_csv(GOB/'growth_optimal_weekly_results.csv')

    log('Running complete hypothesis engine on rebuilt daily-origin sleeves')
    sys.path.insert(0, str(ROOT)); import portfolio_experiment_engine as eng
    registry = []; original_build = eng.build_portfolio; original_boot = eng.paired_block_bootstrap_delta
    def captured_build(*args, **kwargs):
        res = original_build(*args, **kwargs)
        R_arg = kwargs.get('R') if 'R' in kwargs else (args[3] if len(args) > 3 else None)
        setattr(res, '_source_R', R_arg.copy() if isinstance(R_arg, pd.DataFrame) else weekly_sleeves.copy())
        registry.append(res); return res
    def fast_boot(candidate, baseline, *, paths=5000, block=13, seed=20260726):
        return original_boot(candidate, baseline, paths=min(paths, 300), block=block, seed=seed)
    eng.build_portfolio = captured_build; eng.paired_block_bootstrap_delta = fast_boot
    for script in ['run_portfolio_experiments.py','run_combination_experiments.py','run_meta_ensembles.py','run_nested_grid_selection.py']:
        log(f'Executing {script}'); runpy.run_path(str(ROOT/script), run_name='__main__')

    static_rows = pd.read_csv(EXP/'static_grid_results.csv')
    grid_caps = [r for r in registry if r.name == 'grid' and r.category == 'Static grid search']
    for i, res in enumerate(grid_caps[:len(static_rows)]):
        res.name = f'Static grid candidate {i+1:03d}'; res.params = {'weights': json.loads(static_rows.loc[i,'weights'])}
    unique = {}
    for res in registry:
        if res.name.startswith('g') and res.category == 'grid':
            continue
        if res.empirical_status.startswith('Model-based'):
            continue
        unique[res.name] = res
    log(f'Captured {len(unique)} unique portfolio paths')

    daily_unscaled, source_quality, audit_rows = {}, {}, []
    for j, (name, res) in enumerate(unique.items(), start=1):
        source_R = getattr(res, '_source_R', weekly_sleeves)
        ret, status, audit = build_daily_from_result(res, source_R, daily_sleeves, weekly_sleeves, rf_daily)
        daily_unscaled[name] = ret; source_quality[name] = status
        audit_rows.append({'Experiment': name, 'Source quality': status, **audit})
        if j % 100 == 0: log(f'Expanded {j} strategies to daily P&L')

    meta_path = EXP/'meta_ensemble_returns.csv'
    if meta_path.exists():
        meta_week = pd.read_csv(meta_path, parse_dates=['Date']).set_index('Date')
        baseline_name = 'Baseline: current 40/25/12/10/8/5'
        base_shape = daily_unscaled[baseline_name] - rf_daily.reindex(master).fillna(0.0)
        for name in meta_week.columns:
            target_ex = meta_week[name] - rf_weekly.reindex(meta_week.index).fillna(0.0)
            ex_daily = bridge_daily_to_weekly(base_shape, target_ex, master)
            daily_unscaled[name] = rf_daily.reindex(master).fillna(0.0) + ex_daily
            source_quality[name] = 'Hybrid: genuine weekly meta path bridged over baseline daily shape'
            audit_rows.append({'Experiment': name, 'Source quality': source_quality[name],
                               'Direct columns': '', 'Bridged columns': 'meta strategy path'})
    pd.DataFrame(audit_rows).to_csv(OUT/'strategy_daily_source_quality.csv', index=False)

    daily_scaled, scales = {}, {}
    for name, r in daily_unscaled.items():
        scaled, k = equal_vol_daily(r, rf_daily); daily_scaled[name] = scaled; scales[name] = k
    strategy_df = pd.DataFrame(daily_scaled, index=master).loc[:EVAL_END]
    strategy_df.to_csv(OUT/'all_daily_strategy_pnls.csv')
    baseline_name = 'Baseline: current 40/25/12/10/8/5'
    base = strategy_df[baseline_name]
    eval_idx = strategy_df.loc[EVAL_START:EVAL_END].dropna(subset=[baseline_name]).index
    base_eval = base.reindex(eval_idx); rf_eval = rf_daily.reindex(eval_idx).fillna(0.0)

    result_rows, diffs = [], {}
    for name in strategy_df.columns:
        r = strategy_df[name].reindex(eval_idx)
        pair = pd.concat([r.rename('r'), base_eval.rename('b')], axis=1).dropna(); d = pair.r-pair.b
        diffs[name] = d.reindex(eval_idx); m = perf(pair.r, rf_eval.reindex(pair.index)); bm = perf(pair.b, rf_eval.reindex(pair.index))
        result_rows.append({'Experiment': name, 'Source quality': source_quality.get(name, ''),
                            'Daily equal-vol scale': scales.get(name, np.nan), **m,
                            'CAGR uplift vs baseline': m['CAGR']-bm['CAGR'],
                            'Mean daily PnL difference': float(d.mean()),
                            'Stdev daily PnL difference': float(d.std(ddof=1)),
                            'Daily t-stat requested formula': requested_t(d),
                            'Daily Newey-West t-stat 5 lags': newey_west_t(d, 5),
                            'Daily Newey-West t-stat 20 lags': newey_west_t(d, 20)})
    results = pd.DataFrame(result_rows)
    weekly_strategy = (1.0+strategy_df).resample('W-FRI').prod()-1.0; weekly_base = weekly_strategy[baseline_name]
    results['Weekly t-stat from same daily P&Ls'] = results['Experiment'].map({
        name: requested_t((weekly_strategy[name]-weekly_base).loc[EVAL_START:EVAL_END]) for name in weekly_strategy.columns})

    meta_rows = []
    for file in EXP.glob('*results.csv'):
        try: df = pd.read_csv(file)
        except Exception: continue
        if 'Experiment' not in df.columns: continue
        for _, row in df.iterrows():
            meta_rows.append({'Experiment': row.get('Experiment'),
                              'Category': row.get('Category', row.get('Method','')),
                              'Hypothesis': row.get('Hypothesis','')})
    meta_df = pd.DataFrame(meta_rows).dropna(subset=['Experiment']).drop_duplicates('Experiment', keep='first')
    results = results.merge(meta_df, on='Experiment', how='left')
    results.loc[results['Experiment'].str.startswith('Static grid candidate'), 'Category'] = 'Static grid search'

    diff_df = pd.DataFrame(diffs, index=eval_idx)
    observed = results.set_index('Experiment')['Daily t-stat requested formula']
    empirical_cols = [c for c in diff_df.columns if c != baseline_name and np.isfinite(observed.get(c, np.nan))]
    log(f'Running daily max-t reality check over {len(empirical_cols)} strategies')
    pvals, maxima = max_t_block_pvalues(diff_df[empirical_cols], observed.reindex(empirical_cols), block=20, paths=750)
    results['Daily full-search max-t adjusted p'] = results['Experiment'].map(pvals)
    pd.Series(maxima, name='Maximum t-stat under centered 20-day block-bootstrap null').to_csv(OUT/'daily_max_t_null_distribution.csv', index=False)
    results = results.sort_values('Daily t-stat requested formula', ascending=False)
    results.to_csv(OUT/'all_daily_strategy_results.csv', index=False); results.head(100).to_csv(OUT/'daily_strategy_shortlist.csv', index=False)
    family = results.groupby(results['Category'].fillna('Unclassified')).agg(
        Variants=('Experiment','count'), Best_CAGR=('CAGR','max'), Best_CAGR_uplift=('CAGR uplift vs baseline','max'),
        Best_daily_t=('Daily t-stat requested formula','max'), Best_NW20_t=('Daily Newey-West t-stat 20 lags','max'),
        Minimum_search_adjusted_p=('Daily full-search max-t adjusted p','min')).sort_values('Best_daily_t', ascending=False)
    family.to_csv(OUT/'daily_hypothesis_family_summary.csv')
    top_name = results.iloc[0]['Experiment']
    pd.DataFrame({'Baseline': base_eval, 'Top strategy': strategy_df[top_name].reindex(eval_idx),
                  'PnL difference': strategy_df[top_name].reindex(eval_idx)-base_eval}).to_csv(OUT/'top_strategy_daily_pnl_diff.csv')

    option_rows = []
    for code in ['BXM','PUT','PPUT','CLL','VXTH','BXMD']:
        px = fetch_cboe_benchmark(code)
        if px is None: continue
        raw_opt = px.reindex(master).ffill().pct_change(); opt_sleeve, _, _ = weekly_scale_daily(raw_opt)
        for allocation in [0.05, 0.10, 0.20]:
            w = BASE_WEIGHTS.copy()*(1-allocation)
            combined_raw = daily_sleeves.fillna(0.0).mul(w, axis=1).sum(axis=1) + allocation*opt_sleeve.fillna(0.0)
            combined_scaled, _, _ = weekly_scale_daily(combined_raw, target=TARGET_VOL)
            r = rf_daily + combined_scaled - BASE_ANNUAL_COST/TRADING_DAYS; r, k = equal_vol_daily(r, rf_daily)
            pair = pd.concat([r.rename('r'), base.rename('b')], axis=1).loc[EVAL_START:EVAL_END].dropna()
            if len(pair) < 1000: continue
            d = pair.r-pair.b; mm = perf(pair.r, rf_daily.reindex(pair.index).fillna(0.0)); bb = perf(pair.b, rf_daily.reindex(pair.index).fillna(0.0))
            option_rows.append({'Option benchmark': code, 'Risk allocation': allocation, **mm,
                                'CAGR uplift vs baseline': mm['CAGR']-bb['CAGR'],
                                'Daily t-stat requested formula': requested_t(d),
                                'Daily Newey-West t-stat 20 lags': newey_west_t(d,20), 'Equal-vol scale': k})
    pd.DataFrame(option_rows).to_csv(OUT/'genuine_cboe_option_benchmark_tests.csv', index=False)

    pd.DataFrame(SOURCE_MANIFEST).to_csv(OUT/'source_manifest.csv', index=False)
    methodology = {
        'Study period': [str(EVAL_START.date()), str(EVAL_END.date())],
        'Observation frequency': 'Genuine daily portfolio mark-to-market dates from the Fama-French US trading calendar',
        'Rebalance frequency': 'Weekly; weights and leverage are constant within each week',
        'T-stat formula': '(mean(candidate daily PnL - baseline daily PnL) / sample stdev(diff)) * sqrt(number of paired daily observations)',
        'Equal-volatility rule': 'Every strategy is ex-post rescaled to exactly 32.5% annualized daily volatility for comparison.',
        'Direct sleeves': ['QQQ price','Fama-French market','Fama-French HML/RMW/CMA/Mom composite','US5 futures','Gold futures','Corn futures','Silver futures','Bitcoin spot'],
        'Trend reconstruction': 'Contract-level multi-horizon time-series momentum proxy across global equity-index, rates, FX and commodity futures from pysystemtrade daily data.',
        'Derived strategy treatment': 'Strategies whose source return matrix modifies or adds weekly sleeves are bridged across each week using the corresponding genuine daily sleeve shape; source quality is reported per strategy.',
        'Inference warning': 'Daily P&L differences can be serially correlated. Newey-West t-stats and a 20-day moving-block max-t reality check accompany the requested statistic.'}
    (OUT/'methodology.json').write_text(json.dumps(methodology, indent=2), encoding='utf-8')
    summary = {'daily_observations': int(len(eval_idx)), 'strategies': int(len(results)),
               'baseline_cagr': float(results.loc[results.Experiment==baseline_name,'CAGR'].iloc[0]),
               'top_strategy': str(top_name), 'top_cagr': float(results.iloc[0]['CAGR']),
               'top_cagr_uplift': float(results.iloc[0]['CAGR uplift vs baseline']),
               'top_daily_t': float(results.iloc[0]['Daily t-stat requested formula']),
               'top_daily_nw20_t': float(results.iloc[0]['Daily Newey-West t-stat 20 lags']),
               'top_search_adjusted_p': float(results.iloc[0]['Daily full-search max-t adjusted p']),
               'direct_daily_strategies': int((results['Source quality']=='Direct daily source P&L').sum()),
               'hybrid_daily_strategies': int((results['Source quality']!='Direct daily source P&L').sum())}
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    (OUT/'README.md').write_text('# Genuine daily-return portfolio study\n\n'+json.dumps(summary, indent=2), encoding='utf-8')
    log('Study complete'); print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
