from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

OUT = Path('/tmp/excluded_assets_output')
OUT.mkdir(parents=True, exist_ok=True)
START = pd.Timestamp('1999-01-01')
END_EXCLUSIVE = pd.Timestamp('2026-07-02')

TICKERS = {
    'EFA': 'Developed markets ex-US equity',
    'EFV': 'Developed ex-US value equity',
    'EEM': 'Emerging-markets equity',
    'IWM': 'US small-cap equity',
    'IWN': 'US small-cap value equity',
    'RSP': 'S&P 500 equal-weight equity',
    'VNQ': 'US listed real estate / REITs',
    'LQD': 'US investment-grade corporate bonds',
    'HYG': 'US high-yield corporate bonds',
    'TIP': 'US inflation-linked Treasuries',
    'TLT': 'US long-duration Treasuries',
    'EMB': 'USD emerging-market sovereign debt',
    'BNDX': 'Global ex-US investment-grade bonds, USD hedged',
    'DBC': 'Broad commodity futures basket',
    'UUP': 'Long US dollar index',
    'PFF': 'US preferred securities',
}

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 excluded-assets-study/1.0'})


def fetch(ticker: str) -> tuple[pd.Series, str]:
    period1 = int(START.timestamp())
    period2 = int(END_EXCLUSIVE.timestamp())
    url = (
        'https://query1.finance.yahoo.com/v8/finance/chart/'
        f'{ticker}?period1={period1}&period2={period2}&interval=1d&events=history'
    )
    last = None
    for attempt in range(5):
        try:
            response = session.get(url, timeout=90)
            response.raise_for_status()
            payload = response.json()['chart']['result'][0]
            index = pd.to_datetime(
                payload['timestamp'], unit='s', utc=True
            ).tz_convert(None).normalize()
            quote = payload['indicators']['quote'][0]['close']
            adjusted = (
                payload.get('indicators', {})
                .get('adjclose', [{}])[0]
                .get('adjclose')
            )
            values = adjusted if adjusted and len(adjusted) == len(quote) else quote
            series = pd.Series(values, index=index, name=ticker, dtype=float)
            series = series.dropna().sort_index()
            if series.empty:
                raise RuntimeError('empty series')
            return series, url
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f'{ticker}: {last}')


def main() -> None:
    prices = {}
    manifest = []
    for ticker, description in TICKERS.items():
        try:
            series, url = fetch(ticker)
            prices[ticker] = series
            manifest.append({
                'Ticker': ticker,
                'Description': description,
                'Status': 'downloaded',
                'First date': series.index.min(),
                'Last date': series.index.max(),
                'Observations': len(series),
                'URL': url,
            })
            print(ticker, series.index.min().date(), series.index.max().date(), len(series), flush=True)
        except Exception as exc:
            manifest.append({
                'Ticker': ticker,
                'Description': description,
                'Status': f'failed: {exc}',
                'First date': None,
                'Last date': None,
                'Observations': 0,
                'URL': '',
            })
            print('FAILED', ticker, exc, flush=True)

    price_frame = pd.DataFrame(prices).sort_index()
    return_frame = price_frame.pct_change()
    price_frame.to_csv(OUT / 'excluded_asset_adjusted_prices.csv')
    return_frame.to_csv(OUT / 'excluded_asset_total_returns.csv')
    pd.DataFrame(manifest).to_csv(OUT / 'excluded_asset_manifest.csv', index=False)

    summary = {
        'successful_tickers': int(len(prices)),
        'requested_tickers': int(len(TICKERS)),
        'latest_date': str(price_frame.dropna(how='all').index.max().date()) if prices else None,
    }
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
