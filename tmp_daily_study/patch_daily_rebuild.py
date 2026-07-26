from pathlib import Path

p = Path('/mnt/data/daily_rebuild_and_study.py')
s = p.read_text(encoding='utf-8')
old1 = """def parse_adjusted_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    valuecol = 'price' if 'price' in df.columns else df.columns[-1]
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df[valuecol], errors='coerce').values, index=idx, name=path.stem)
    return s[~s.index.duplicated(keep='last')].sort_index().dropna()
"""
new1 = """def parse_adjusted_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    valuecol = 'price' if 'price' in df.columns else df.columns[-1]
    # Source files use heterogeneous UTC wall-clock stamps (often 23:00, but also
    # exchange-specific intraday times).  The study is daily, so join by trading
    # date rather than by exact timestamp.  Keep the final mark when a file has
    # multiple observations on the same date.
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None).dt.normalize()
    s = pd.Series(pd.to_numeric(df[valuecol], errors='coerce').values, index=idx, name=path.stem)
    return s.groupby(level=0).last().sort_index().dropna()
"""
old2 = """def parse_multiple_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    if 'PRICE' not in df.columns:
        raise ValueError(f'{path} lacks PRICE column')
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df['PRICE'], errors='coerce').values, index=idx, name=path.stem)
    return s[~s.index.duplicated(keep='last')].sort_index().dropna()
"""
new2 = """def parse_multiple_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    datecol = df.columns[0]
    if 'PRICE' not in df.columns:
        raise ValueError(f'{path} lacks PRICE column')
    idx = pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None).dt.normalize()
    s = pd.Series(pd.to_numeric(df['PRICE'], errors='coerce').values, index=idx, name=path.stem)
    return s.groupby(level=0).last().sort_index().dropna()
"""
if old1 not in s or old2 not in s:
    raise RuntimeError('Expected futures parser blocks were not found')
s = s.replace(old1, new1).replace(old2, new2)
p.write_text(s, encoding='utf-8')
print('Patched futures timestamps to normalized trading dates')
