#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /mnt/data
sudo chown -R "$USER":"$USER" /mnt/data
mkdir -p /tmp/artifact

cat .tmp-daily-portfolio-study/code_payload.part00 \
    .tmp-daily-portfolio-study/code_payload.part01a \
    .tmp-daily-portfolio-study/code_payload.part01b \
    .tmp-daily-portfolio-study/code_payload.part02aa \
    .tmp-daily-portfolio-study/code_payload.part02ab \
    .tmp-daily-portfolio-study/code_payload.part02b \
    .tmp-daily-portfolio-study/code_payload.part03 \
    .tmp-daily-portfolio-study/code_payload.part04 \
    | tr -d '\n\r ' > /tmp/code_payload.b64
base64 --decode /tmp/code_payload.b64 > /tmp/code_payload.zip
echo "16906abfffc0f4b4c2a2073c97d6a27aba74ca80ffdf34a6db63ed6aa9a847d5  /tmp/code_payload.zip" | sha256sum --check
unzip -t /tmp/code_payload.zip
unzip -q /tmp/code_payload.zip -d /mnt/data
cp .tmp-daily-portfolio-study/daily_rebuild_and_study.py /mnt/data/daily_rebuild_and_study.py

python - <<'PY'
from pathlib import Path

runner = Path('/mnt/data/daily_rebuild_and_study.py')
text = runner.read_text(encoding='utf-8')
text = text.replace(
    "pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None)",
    "pd.to_datetime(df[datecol], errors='coerce').dt.tz_localize(None).dt.normalize()",
)
runner.write_text(text, encoding='utf-8')

engine = Path('/mnt/data/portfolio_experiment_engine.py')
text = engine.read_text(encoding='utf-8')
text = text.replace(
    """    if s <= 0:
        raise ValueError('weights sum to zero')
    return w / s
""",
    """    if s <= 0:
        if len(w) == 0:
            raise ValueError('empty asset set')
        return pd.Series(1.0 / len(w), index=w.index)
    return w / s
""",
)
engine.write_text(text, encoding='utf-8')
PY

python -m py_compile /mnt/data/*.py
python /mnt/data/daily_rebuild_and_study.py 2>&1 | tee /tmp/artifact/study.log
