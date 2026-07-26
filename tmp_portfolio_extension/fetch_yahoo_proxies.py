from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

OUT = Path('/tmp/portfolio_extension_output')
OUT.mkdir(parents=True, exist_ok=True)
START = pd.Timestamp('2010-01-01')
END_EXCLUSIVE = pd.Timestamp('2026-07-02')

# Frozen research instruments mapped to liquid Yahoo futures, indices, FX spots, or government-bond ETFs.
# invert=True converts foreign-currency-per-USD quotes into USD-per-foreign-currency returns.
PROXIES = {
    'SP500': ('ES=F', False, 'US equity-index future'),
    'NASDAQ': ('NQ=F', False, 'US technology-index future'),
    'DOW': ('YM=F', False, 'US Dow future'),
    'FTSE100': ('^FTSE', False, 'FTSE 100 cash-index proxy'),
    'DAX': ('^GDAXI', False, 'DAX cash-index proxy'),
    'HANG': ('^HSI', False, 'Hang Seng cash-index proxy'),
    'US2': ('ZT=F', False, 'US 2-year Treasury future'),
    'US5': ('ZF=F', False, 'US 5-year Treasury future'),
    'US10': ('ZN=F', False, 'US 10-year Treasury future'),
    'US20': ('ZB=F', False, 'US long-bond future proxy'),
    'BUND': ('EXX6.DE', False, 'Long German-government-bond ETF proxy'),
    'GILT': ('IGLT.L', False, 'UK gilt ETF proxy'),
    'JGB': ('2561.T', False, 'Japan government-bond ETF proxy'),
    'CAD10': ('XGB.TO', False, 'Canadian government-bond ETF proxy'),
    'EUR': ('EURUSD=X', False, 'EURUSD spot'),
    'GBP': ('GBPUSD=X', False, 'GBPUSD spot'),
    'JPY': ('JPY=X', True, 'Inverted USDJPY spot'),
    'CHF': ('CHF=X', True, 'Inverted USDCHF spot'),
    'AUD': ('AUDUSD=X', False, 'AUDUSD spot'),
    'CAD': ('CAD=X', True, 'Inverted USDCAD spot'),
    'NZD': ('NZDUSD=X', False, 'NZDUSD spot'),
    'MXP': ('MXN=X', True, 'Inverted USDMXN spot'),
    'PLN': ('PLN=X', True, 'Inverted USDPLN spot'),
    'SEK': ('SEK=X', True, 'Inverted USDSEK spot'),
    'NOK': ('NOK=X', True, 'Inverted USDNOK spot'),
    'GOLD': ('GC=F', False, 'Gold future'),
    'SILVER': ('SI=F', False, 'Silver future'),
    'COPPER': ('HG=F', False, 'Copper future'),
    'CRUDE_W': ('CL=F', False, 'WTI crude future'),
    'BRENT_W': ('BZ=F', False, 'Brent crude future'),
    'GAS_US': ('NG=F', False, 'US natural-gas future'),
    'CORN': ('ZC=F', False, 'Corn future'),
    'WHEAT': ('ZW=F', False, 'Wheat future'),
    'SOYBEAN': ('ZS=F', False, 'Soybean future'),
    'SOYMEAL': ('ZM=F', False, 'Soybean-meal future'),
    'COCOA': ('CC=F', False, 'Cocoa future'),
    'LIVECOW': ('LE=F', False, 'Live-cattle future'),
    'LEANHOG': ('HE=F', False, 'Lean-hogs future'),
}
STYLE_TICKERS = ['SPY','VTV','VUG','QUAL','MTUM','USMV','IUSV','IUSG','VLUE']

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 portfolio-extension-proxy/1.0'})


def fetch(ticker: str) -> tuple[pd.Series, str]:
    p1 = int(START.timestamp())
    p2 = int(END_EXCLUSIVE.timestamp())
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}&interval=1d&events=history'
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            payload = r.json()['chart']['result'][0]
            idx = pd.to_datetime(payload['timestamp'], unit='s', utc=True).tz_convert(None).normalize()
            quote = payload['indicators']['quote'][0]['close']
            adj = payload.get('indicators', {}).get('adjclose', [{}])[0].get('adjclose')
            values = adj if adj and len(adj) == len(quote) else quote
            s = pd.Series(values, index=idx, name=ticker, dtype=float).dropna().sort_index()
            if s.empty:
                raise RuntimeError('empty series')
            return s, url
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f'{ticker}: {last}')


def main() -> None:
    returns = {}
    prices = {}
    manifest = []
    for instrument, (ticker, invert, note) in PROXIES.items():
        try:
            s, url = fetch(ticker)
            if invert:
                s = 1.0 / s
            prices[instrument] = s
            returns[instrument] = s.pct_change()
            manifest.append({
                'Instrument': instrument, 'Ticker': ticker, 'Status': 'downloaded',
                'First date': s.index.min(), 'Last date': s.index.max(),
                'Observations': len(s), 'Invert': invert, 'Notes': note, 'URL': url,
            })
            print(instrument, ticker, s.index.min().date(), s.index.max().date(), len(s), flush=True)
        except Exception as exc:
            manifest.append({
                'Instrument': instrument, 'Ticker': ticker, 'Status': f'failed: {exc}',
                'First date': None, 'Last date': None, 'Observations': 0,
                'Invert': invert, 'Notes': note, 'URL': '',
            })
            print('FAILED', instrument, ticker, exc, flush=True)

    style_prices = {}
    for ticker in STYLE_TICKERS:
        try:
            s, url = fetch(ticker)
            style_prices[ticker] = s
            manifest.append({
                'Instrument': f'STYLE_{ticker}', 'Ticker': ticker, 'Status': 'downloaded',
                'First date': s.index.min(), 'Last date': s.index.max(),
                'Observations': len(s), 'Invert': False, 'Notes': 'Style-proxy regression input', 'URL': url,
            })
        except Exception as exc:
            manifest.append({
                'Instrument': f'STYLE_{ticker}', 'Ticker': ticker, 'Status': f'failed: {exc}',
                'First date': None, 'Last date': None, 'Observations': 0,
                'Invert': False, 'Notes': 'Style-proxy regression input', 'URL': '',
            })

    pd.DataFrame(prices).sort_index().to_csv(OUT / 'proxy_prices.csv')
    pd.DataFrame(returns).sort_index().to_csv(OUT / 'proxy_returns.csv')
    pd.DataFrame(style_prices).sort_index().to_csv(OUT / 'style_proxy_prices.csv')
    pd.DataFrame(manifest).to_csv(OUT / 'proxy_manifest.csv', index=False)

    manifest_df = pd.DataFrame(manifest)
    summary = {
        'trend_proxy_successes': int(((manifest_df.Instrument.isin(PROXIES)) & manifest_df.Status.eq('downloaded')).sum()),
        'trend_proxy_failures': int(((manifest_df.Instrument.isin(PROXIES)) & ~manifest_df.Status.eq('downloaded')).sum()),
        'style_proxy_successes': int((manifest_df.Instrument.str.startswith('STYLE_') & manifest_df.Status.eq('downloaded')).sum()),
        'latest_proxy_date': str(pd.DataFrame(prices).dropna(how='all').index.max().date()) if prices else None,
    }
    (OUT / 'proxy_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
