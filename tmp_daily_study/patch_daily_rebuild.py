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

marker = """def clone_pysystemtrade() -> Path:
"""
aligner = """def align_pnl_to_master(s: pd.Series, master: pd.DatetimeIndex) -> pd.Series:
    # The portfolio uses a common U.S. trading calendar.  Global futures may mark
    # on a date when that calendar is closed; aggregate those P&Ls into the next
    # available portfolio mark instead of silently dropping them.
    s = s.dropna().sort_index()
    s = s.loc[(s.index >= master.min()) & (s.index <= master.max())]
    if s.empty:
        return pd.Series(0.0, index=master, name=s.name)
    positions = master.searchsorted(s.index, side='left')
    valid = positions < len(master)
    aligned = pd.Series(s.to_numpy()[valid], index=master[positions[valid]], name=s.name)
    return aligned.groupby(level=0).sum().reindex(master).fillna(0.0)


"""
if marker not in s:
    raise RuntimeError('clone_pysystemtrade marker not found')
s = s.replace(marker, aligner + marker, 1)
old_trend = "instrument_returns[inst] = futures_raw_return(repo, inst).reindex(master).fillna(0.0)"
new_trend = "instrument_returns[inst] = align_pnl_to_master(futures_raw_return(repo, inst), master)"
old_direct = "futures_raw[sleeve] = futures_raw_return(repo, inst).reindex(master).fillna(0.0)"
new_direct = "futures_raw[sleeve] = align_pnl_to_master(futures_raw_return(repo, inst), master)"
if old_trend not in s or old_direct not in s:
    raise RuntimeError('Expected futures alignment call sites were not found')
s = s.replace(old_trend, new_trend).replace(old_direct, new_direct)

p.write_text(s, encoding='utf-8')
print('Patched futures timestamps and off-calendar P&L alignment')
