from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path('/tmp/portfolio_extension')
OUT = Path('/tmp/portfolio_extension_output')
ROOT.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

START = pd.Timestamp('1999-01-01')
END_EXCLUSIVE = pd.Timestamp('2026-07-01')
STUDY_END = pd.Timestamp('2026-06-30')
TRADING_DAYS = 252.0
SLEEVE_VOL = 0.10

GROUPS = {
    'Equity': ['SP500','NASDAQ','DOW','FTSE100','DAX','HANG'],
    'Rates': ['US2','US5','US10','US20','BUND','GILT','JGB','CAD10'],
    'FX': ['EUR','GBP','JPY','CHF','AUD','CAD','NZD','MXP','PLN','SEK','NOK'],
    'Commodities': ['GOLD','SILVER','COPPER','CRUDE_W','BRENT_W','GAS_US','CORN','WHEAT','SOYBEAN','SOYMEAL','COCOA','LIVECOW','LEANHOG'],
}
DIRECT_FUTURES = {'Rates_US5': 'US5', 'Gold': 'GOLD', 'Corn': 'CORN', 'Silver': 'SILVER'}

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 portfolio-extension-research/1.0'})
manifest: list[dict] = []


def log(msg: str) -> None:
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def fetch_bytes(url: str, timeout: int = 90, tries: int = 4) -> bytes:
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as exc:
            last = exc
            time.sleep(2 ** i)
    raise RuntimeError(f'Failed to fetch {url}: {last}')


def record(name: str, url: str, status: str, detail: str = '') -> None:
    manifest.append({'Source': name, 'URL': url, 'Status': status, 'Detail': detail})


def load_french_zip(url: str, expected: list[str], name: str) -> pd.DataFrame:
    raw = fetch_bytes(url)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    member = zf.namelist()[0]
    text = zf.read(member).decode('latin1')
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip(',')
        if stripped.startswith('Mkt-RF') or stripped.startswith('Mom'):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if 'Mom' in line and ',' in line:
                start = i
                break
    if start is None:
        raise RuntimeError(f'Could not identify header in {name}')
    data = [lines[start]]
    for line in lines[start + 1:]:
        first = line.split(',')[0].strip()
        if len(first) != 8 or not first.isdigit():
            break
        data.append(line)
    df = pd.read_csv(io.StringIO('\n'.join(data)))
    df.columns = [str(c).strip() or 'Date' for c in df.columns]
    if 'Date' not in df.columns:
        df = df.rename(columns={df.columns[0]: 'Date'})
    df['Date'] = pd.to_datetime(df['Date'].astype(str), format='%Y%m%d')
    df = df.set_index('Date').sort_index()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce') / 100.0
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise RuntimeError(f'{name} missing columns {missing}: {list(df.columns)}')
    record(name, url, 'downloaded', f'{member}; max date {df.index.max().date()}')
    return df


def yahoo_close(ticker: str, name: str) -> pd.Series:
    p1 = int(START.timestamp())
    p2 = int(END_EXCLUSIVE.timestamp())
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}&interval=1d&events=history'
    payload = json.loads(fetch_bytes(url).decode('utf-8'))
    result = payload['chart']['result'][0]
    idx = pd.to_datetime(result['timestamp'], unit='s', utc=True).tz_convert(None).normalize()
    values = result['indicators']['quote'][0]['close']
    s = pd.Series(values, index=idx, name=name, dtype=float).dropna().sort_index()
    record(name, url, 'downloaded', f'{len(s)} closes; max date {s.index.max().date()}')
    return s


def parse_adjusted(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    dcol = df.columns[0]
    vcol = 'price' if 'price' in df.columns else df.columns[-1]
    idx = pd.to_datetime(df[dcol], errors='coerce').dt.tz_localize(None).dt.normalize()
    s = pd.Series(pd.to_numeric(df[vcol], errors='coerce').to_numpy(), index=idx, name=path.stem)
    return s.groupby(level=0).last().sort_index().dropna()


def parse_multiple(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    dcol = df.columns[0]
    if 'PRICE' not in df.columns:
        raise RuntimeError(f'{path} lacks PRICE')
    idx = pd.to_datetime(df[dcol], errors='coerce').dt.tz_localize(None).dt.normalize()
    s = pd.Series(pd.to_numeric(df['PRICE'], errors='coerce').to_numpy(), index=idx, name=path.stem)
    return s.groupby(level=0).last().sort_index().dropna()


def futures_raw(repo: Path, instrument: str) -> pd.Series:
    adj = parse_adjusted(repo / 'data/futures/adjusted_prices_csv' / f'{instrument}.csv')
    cur = parse_multiple(repo / 'data/futures/multiple_prices_csv' / f'{instrument}.csv')
    df = pd.concat([adj.rename('adj'), cur.rename('px')], axis=1).sort_index()
    r = df['adj'].diff() / df['px'].shift(1).abs()
    r = r.replace([np.inf, -np.inf], np.nan)
    r.name = instrument
    return r


def align_pnl(s: pd.Series, master: pd.DatetimeIndex) -> pd.Series:
    s = s.dropna().sort_index()
    s = s.loc[(s.index >= master.min() - pd.Timedelta(days=10)) & (s.index <= master.max())]
    positions = master.searchsorted(s.index, side='left')
    valid = positions < len(master)
    aligned = pd.Series(s.to_numpy()[valid], index=master[positions[valid]], name=s.name)
    return aligned.groupby(level=0).sum().reindex(master).fillna(0.0)


def build_trend_market(repo: Path, master: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = {}
    audit = []
    for group, instruments in GROUPS.items():
        for inst in instruments:
            try:
                raw = futures_raw(repo, inst)
                data[inst] = align_pnl(raw, master)
                audit.append({'Group': group, 'Instrument': inst, 'Status': 'included', 'First date': raw.dropna().index.min(), 'Last date': raw.dropna().index.max(), 'Observations': raw.notna().sum()})
            except Exception as exc:
                audit.append({'Group': group, 'Instrument': inst, 'Status': f'failed: {exc}', 'First date': None, 'Last date': None, 'Observations': 0})
                raise
    return pd.DataFrame(data, index=master), pd.DataFrame(audit)


def trend_raw_from_markets(R: pd.DataFrame) -> pd.Series:
    fast = R.ewm(halflife=20, adjust=False, min_periods=20).std(bias=False).shift(1) * math.sqrt(TRADING_DAYS)
    slow = R.ewm(halflife=100, adjust=False, min_periods=100).std(bias=False).shift(1) * math.sqrt(TRADING_DAYS)
    expv = R.expanding(min_periods=60).std(ddof=1).shift(1) * math.sqrt(TRADING_DAYS)
    parts = pd.concat({'fast': fast, 'slow': 0.8 * slow, 'exp': 0.55 * expv}, axis=1)
    vol = pd.DataFrame({c: parts.xs(c, level=1, axis=1).max(axis=1) for c in R.columns}, index=R.index)
    fallback = R.expanding(min_periods=20).std(ddof=1).shift(1) * math.sqrt(TRADING_DAYS)
    vol = vol.fillna(fallback).fillna(0.15).clip(lower=0.04)
    scaled = R * (SLEEVE_VOL / vol)
    signals = [np.sign(R.rolling(h, min_periods=max(10, h // 2)).sum()).shift(1) for h in [21, 63, 126, 252]]
    signal = sum(signals) / len(signals)
    pnl = scaled * signal
    group_pnl = {}
    for group, instruments in GROUPS.items():
        group_pnl[group] = pnl[instruments].mean(axis=1)
    return pd.DataFrame(group_pnl).mean(axis=1).rename('Trend')


def main() -> None:
    log('Downloading current Fama-French factors')
    ff5 = load_french_zip(
        'https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip',
        ['Mkt-RF','HML','RMW','CMA','RF'], 'Fama-French 5 factors daily')
    mom = load_french_zip(
        'https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip',
        ['Mom'], 'Fama-French momentum daily')
    ff = ff5.join(mom[['Mom']], how='inner').sort_index()
    ff = ff.loc[START:STUDY_END]
    ff.to_csv(OUT / 'current_fama_french_daily.csv')

    log('Downloading QQQ and Bitcoin closes')
    qqq = yahoo_close('QQQ', 'QQQ')
    btc = yahoo_close('BTC-USD', 'BTC-USD')
    qqq.to_csv(OUT / 'current_qqq_close.csv', header=True)
    btc.to_csv(OUT / 'current_btc_close.csv', header=True)

    master = ff.index
    if master.max() < STUDY_END:
        log(f'WARNING: common Fama-French data end at {master.max().date()}, before requested {STUDY_END.date()}')

    log('Cloning current pysystemtrade futures data')
    repo = ROOT / 'pysystemtrade'
    if repo.exists():
        shutil.rmtree(repo)
    subprocess.run(['git','clone','--depth','1','--branch','develop','https://github.com/pst-group/pysystemtrade.git',str(repo)], check=True)
    record('pysystemtrade futures data', 'https://github.com/pst-group/pysystemtrade', 'cloned', 'develop branch')

    log('Building frozen 38-market trend universe')
    trend_markets, audit = build_trend_market(repo, master)
    trend_markets.to_csv(OUT / 'current_trend_market_raw_daily_returns.csv')
    audit.to_csv(OUT / 'current_trend_instrument_audit.csv', index=False)
    trend = trend_raw_from_markets(trend_markets)

    qqq_raw = qqq.reindex(master).ffill().pct_change()
    btc_raw = btc.reindex(master, method='ffill').pct_change()
    btc_raw.loc[btc_raw.index < pd.Timestamp('2017-01-03')] = np.nan

    direct = {}
    for sleeve, inst in DIRECT_FUTURES.items():
        direct[sleeve] = align_pnl(futures_raw(repo, inst), master)

    raw = pd.DataFrame({
        'Growth_QQQ': qqq_raw,
        'Broad_US': ff['Mkt-RF'].reindex(master),
        'Trend': trend.reindex(master),
        'Rates_US5': direct['Rates_US5'],
        'Styles': ff[['HML','RMW','CMA','Mom']].mean(axis=1).reindex(master),
        'Gold': direct['Gold'],
        'Corn': direct['Corn'],
        'Silver': direct['Silver'],
        'Bitcoin': btc_raw,
    }, index=master)
    raw.to_csv(OUT / 'current_raw_sleeve_returns.csv')

    summary = {
        'requested_end': str(STUDY_END.date()),
        'fama_french_max_date': str(ff.index.max().date()),
        'qqq_max_date': str(qqq.index.max().date()),
        'bitcoin_max_date': str(btc.index.max().date()),
        'raw_sleeve_max_date': str(raw.dropna(how='all').index.max().date()),
        'rows': int(len(raw)),
        'trend_markets': int(trend_markets.shape[1]),
        'trend_audit_failures': int((audit['Status'] != 'included').sum()),
    }
    (OUT / 'current_source_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    pd.DataFrame(manifest).to_csv(OUT / 'current_source_manifest.csv', index=False)
    log(json.dumps(summary))


if __name__ == '__main__':
    main()
