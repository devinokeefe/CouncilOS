from pathlib import Path

p = Path('/mnt/data/daily_rebuild_and_study.py')
s = p.read_text(encoding='utf-8')

old_sig = """def build_daily_from_result(res, source_R: pd.DataFrame, daily_sleeves: pd.DataFrame, weekly_sleeves: pd.DataFrame,
                            rf_daily: pd.Series, rf_weekly: pd.Series) -> tuple[pd.Series, str, dict]:
    master = daily_sleeves.index
"""
new_sig = """def build_daily_from_result(res, source_R: pd.DataFrame, daily_sleeves: pd.DataFrame, weekly_sleeves: pd.DataFrame,
                            rf_daily: pd.Series, rf_weekly: pd.Series,
                            extra_daily_shapes: dict[str, pd.Series] | None = None) -> tuple[pd.Series, str, dict]:
    master = daily_sleeves.index
    extra_daily_shapes = extra_daily_shapes or {}
"""
if old_sig not in s:
    raise RuntimeError('build_daily_from_result signature not found')
s = s.replace(old_sig, new_sig, 1)

old_loop = """        if col in daily_sleeves.columns and col in weekly_sleeves.columns:
            base_week = weekly_sleeves[col].reindex(target_week.index)
            paired = pd.concat([base_week.rename('b'), target_week.rename('t')], axis=1).dropna()
            is_direct = len(paired) > 100 and np.nanmax(np.abs(paired.t-paired.b)) < 1e-8
            shape = daily_sleeves[col]
        else:
            is_direct = False
            shape = generic_shape
"""
new_loop = """        if col in daily_sleeves.columns and col in weekly_sleeves.columns:
            base_week = weekly_sleeves[col].reindex(target_week.index)
            paired = pd.concat([base_week.rename('b'), target_week.rename('t')], axis=1).dropna()
            is_direct = len(paired) > 100 and np.nanmax(np.abs(paired.t-paired.b)) < 1e-8
            shape = daily_sleeves[col]
        elif col in extra_daily_shapes:
            # Preserve the exact weekly normalized sleeve used by the hypothesis engine,
            # but distribute it using the genuine daily wrapper P&L shape rather than a
            # generic average-asset bridge.
            is_direct = False
            shape = extra_daily_shapes[col].reindex(master).fillna(0.0)
        else:
            is_direct = False
            shape = generic_shape
"""
if old_loop not in s:
    raise RuntimeError('daily component selection block not found')
s = s.replace(old_loop, new_loop, 1)

old_after_wrapper = """    qqq55_weekly = 0.55*qqq3_weekly + 0.45*cash_weekly

    inputs = weekly_sleeves.copy()
"""
new_after_wrapper = """    qqq55_weekly = 0.55*qqq3_weekly + 0.45*cash_weekly
    wrapper_daily_shapes = {
        '3x QQQ daily-reset wrapper': qqq3_daily - rf_daily.reindex(master).fillna(0.0),
        '55% 3x QQQ plus cash wrapper': (0.55*qqq3_daily + 0.45*rf_daily.reindex(master).fillna(0.0)) - rf_daily.reindex(master).fillna(0.0),
    }

    inputs = weekly_sleeves.copy()
"""
if old_after_wrapper not in s:
    raise RuntimeError('wrapper construction block not found')
s = s.replace(old_after_wrapper, new_after_wrapper, 1)

old_call = """        ret, status, audit = build_daily_from_result(res, source_R, daily_sleeves, weekly_sleeves, rf_daily, rf_weekly)
"""
new_call = """        ret, status, audit = build_daily_from_result(
            res, source_R, daily_sleeves, weekly_sleeves, rf_daily, rf_weekly,
            extra_daily_shapes=wrapper_daily_shapes,
        )
"""
if old_call not in s:
    raise RuntimeError('build_daily_from_result call not found')
s = s.replace(old_call, new_call, 1)

p.write_text(s, encoding='utf-8')
print('Patched leveraged-wrapper variants to use genuine daily wrapper P&L shapes')
