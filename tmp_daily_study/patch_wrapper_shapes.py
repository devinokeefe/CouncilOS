from pathlib import Path

p = Path('/mnt/data/daily_rebuild_and_study.py')
s = p.read_text(encoding='utf-8')

old_sig = """def build_daily_from_result(res, source_R: pd.DataFrame, daily_sleeves: pd.DataFrame, weekly_sleeves: pd.DataFrame,
                            rf_daily: pd.Series, rf_weekly: pd.Series) -> tuple[pd.Series, str, dict]:
    master = daily_sleeves.index
"""
new_sig = """def build_daily_from_result(res, source_R: pd.DataFrame, daily_sleeves: pd.DataFrame, weekly_sleeves: pd.DataFrame,
                            rf_daily: pd.Series, rf_weekly: pd.Series,
                            extra_daily_shapes: dict[str, dict[str, pd.Series]] | None = None) -> tuple[pd.Series, str, dict]:
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
        if is_direct:
            daily_components[col] = shape
            direct_cols.append(col)
        else:
            daily_components[col] = bridge_daily_to_weekly(shape, target_week, master)
            bridged_cols.append(col)
"""
new_loop = """        if col in daily_sleeves.columns and col in weekly_sleeves.columns:
            base_week = weekly_sleeves[col].reindex(target_week.index)
            paired = pd.concat([base_week.rename('b'), target_week.rename('t')], axis=1).dropna()
            is_direct = len(paired) > 100 and np.nanmax(np.abs(paired.t-paired.b)) < 1e-8
            shape = daily_sleeves[col]
        elif col in extra_daily_shapes:
            # The weekly experiment engine normalizes the wrapper's compounded weekly
            # excess return. Reconstruct that same weekly multiplier, apply it to the
            # genuine daily wrapper excess P&L, and book only the small arithmetic-vs-
            # compounded reconciliation residual on the final mark of the week.
            spec = extra_daily_shapes[col]
            daily_shape = spec['daily'].reindex(master).fillna(0.0)
            weekly_base = spec['weekly'].reindex(target_week.index)
            scale = pd.Series(0.0, index=target_week.index)
            good = weekly_base.abs() > 1e-10
            scale.loc[good] = target_week.loc[good] / weekly_base.loc[good]
            scale = scale.replace([np.inf, -np.inf], 0.0).fillna(0.0).clip(-5.0, 5.0)
            mapped_scale = pd.Series(scale.reindex(labels.values).to_numpy(), index=master).fillna(0.0)
            component = daily_shape * mapped_scale
            component_week = component.groupby(labels).sum().reindex(target_week.index).fillna(0.0)
            residual = (target_week - component_week).fillna(0.0)
            for wk, idx in labels.groupby(labels).groups.items():
                if wk in residual.index:
                    component.loc[idx[-1]] += float(residual.loc[wk])
            daily_components[col] = component
            bridged_cols.append(col)
            continue
        else:
            is_direct = False
            shape = generic_shape
        if is_direct:
            daily_components[col] = shape
            direct_cols.append(col)
        else:
            daily_components[col] = bridge_daily_to_weekly(shape, target_week, master)
            bridged_cols.append(col)
"""
if old_loop not in s:
    raise RuntimeError('daily component selection block not found')
s = s.replace(old_loop, new_loop, 1)

old_after_wrapper = """    qqq55_weekly = 0.55*qqq3_weekly + 0.45*cash_weekly

    inputs = weekly_sleeves.copy()
"""
new_after_wrapper = """    qqq55_weekly = 0.55*qqq3_weekly + 0.45*cash_weekly
    wrapper_daily_shapes = {
        '3x QQQ daily-reset wrapper': {
            'daily': qqq3_daily - rf_daily.reindex(master).fillna(0.0),
            'weekly': qqq3_weekly - rf_weekly.reindex(qqq3_weekly.index).fillna(0.0),
        },
        '55% 3x QQQ plus cash wrapper': {
            'daily': (0.55*qqq3_daily + 0.45*rf_daily.reindex(master).fillna(0.0)) - rf_daily.reindex(master).fillna(0.0),
            'weekly': qqq55_weekly - rf_weekly.reindex(qqq55_weekly.index).fillna(0.0),
        },
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
print('Patched leveraged-wrapper variants using exact weekly normalization multipliers and genuine daily P&L shapes')
