from pathlib import Path

p = Path('/mnt/data/portfolio_experiment_engine.py')
s = p.read_text(encoding='utf-8')
old = '''                iv = inverse_vol_estimator(hist[active])
                iv = normalize_static_weights(iv, active)
                inside[cname] = iv
                cluster_returns[cname] = (hist[active].fillna(0) * iv).sum(axis=1)
            cr = pd.DataFrame(cluster_returns)
            civ = inverse_vol_estimator(cr)
            civ = normalize_static_weights(civ, cr.columns)
            candidate = pd.Series(0.0, index=R.columns)
'''
new = '''                iv = inverse_vol_estimator(hist[active])
                if (not np.isfinite(iv).any()) or float(iv.fillna(0.0).sum()) <= 1e-14:
                    iv = pd.Series(1.0 / len(active), index=active)
                else:
                    iv = normalize_static_weights(iv, active)
                inside[cname] = iv
                cluster_returns[cname] = (hist[active].fillna(0) * iv).sum(axis=1)
            cr = pd.DataFrame(cluster_returns)
            if cr.empty:
                out.loc[dt] = current.values
                continue
            civ = inverse_vol_estimator(cr)
            if (not np.isfinite(civ).any()) or float(civ.fillna(0.0).sum()) <= 1e-14:
                civ = pd.Series(1.0 / len(cr.columns), index=cr.columns)
            else:
                civ = normalize_static_weights(civ, cr.columns)
            candidate = pd.Series(0.0, index=R.columns)
'''
if old not in s:
    raise RuntimeError('Expected cluster-equal-risk block was not found')
p.write_text(s.replace(old, new), encoding='utf-8')
print('Patched degenerate inverse-volatility fallback in cluster_equal_risk_weights')
