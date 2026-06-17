# validation/freqtrade — TJR look-ahead bias check (kept artifacts)

> **Headline result: TJR core passed Freqtrade lookahead-analysis — 98 signals, 0 bias.**

These are the **kept artifacts** of an independent look-ahead-bias check of the
live TJR strategy. They were produced in an isolated Freqtrade sandbox that lives
**outside** this repo (`../ft-validation/`, a sibling of `stock-scanner/`, with
its own Python 3.12 venv and downloaded candle data — neither is committed here).
This folder contains only the strategy port and the write-ups; the venv and the
downloaded 5m data are intentionally excluded (see `.gitignore`).

- **What it checks:** the live signal logic in `quant/tjr_strategy.py` was
  re-implemented as a Freqtrade `IStrategy` (`strategies/TjrValidation.py`, which
  cites the source line-by-line) and run through `freqtrade lookahead-analysis`,
  which re-runs the strategy on progressively hidden data and flags any signal
  that depends on candles it shouldn't see.
- **Result:** no bias detected — 98/100 entry signals checked, 0 biased entries,
  0 biased exits, 0 biased indicators. Full output + honest caveats in
  `VALIDATION_REPORT.md`; porting fidelity + every approximation in
  `PORTING_NOTES.md`. The live bot was not modified.

### Reproduce
Recreate the sandbox beside this repo (`../ft-validation/`), copy
`strategies/TjrValidation.py` into `user_data/strategies/`, download data, then:

```bash
freqtrade lookahead-analysis --strategy TjrValidation \
  --config user_data/config.json --userdir user_data \
  --pairs BTC/USDT ETH/USDT --timeframe 5m --timerange 20260101-20260616 \
  --targeted-trade-amount 200 --minimum-trade-amount 50
```

Tooling: Freqtrade 2026.5.1, Python 3.12, Binance `BTC/USDT`+`ETH/USDT` 5m,
window `2026-01-01 → 2026-06-16`. Source ported: `quant/tjr_strategy.py` @ `7be34bb`.

---

## Original sandbox notes

The remainder of this README is the original sandbox documentation, kept for
reference. Paths like `/Users/amirsalah/ft-validation/`, `.venv/`, and
`user_data/data/` refer to that out-of-repo sandbox, not this folder.

## Layout
```
ft-validation/
├── .venv/                         # isolated Python 3.12 venv (freqtrade 2026.5.1)
├── README.md                      # this file
├── PORTING_NOTES.md               # how the TJR logic was mapped + every approximation
├── VALIDATION_REPORT.md           # the verdict, full tool output, caveats
└── user_data/
    ├── config.json                # Binance USDT, BTC/ETH, 5m, dry-run
    ├── data/binance/              # downloaded 5m candles (feather)
    ├── strategies/TjrValidation.py# the port (cites quant/tjr_strategy.py line by line)
    └── lookahead_tjr.csv          # exported result
```

## Reproduce
```bash
cd /Users/amirsalah/ft-validation
# Python 3.12 (Freqtrade does not support 3.14, the only system python here):
#   brew install python@3.12
# /opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
# .venv/bin/pip install freqtrade

# data (already downloaded):
.venv/bin/freqtrade download-data --exchange binance \
  --pairs BTC/USDT ETH/USDT --timeframe 5m --timerange 20260101-20260616 \
  --userdir user_data

# sanity backtest (produces trades):
.venv/bin/freqtrade backtesting --strategy TjrValidation \
  --config user_data/config.json --userdir user_data \
  --pairs BTC/USDT ETH/USDT --timeframe 5m --timerange 20260101-20260616

# the bias check:
.venv/bin/freqtrade lookahead-analysis --strategy TjrValidation \
  --config user_data/config.json --userdir user_data \
  --pairs BTC/USDT ETH/USDT --timeframe 5m --timerange 20260101-20260616 \
  --targeted-trade-amount 200 --minimum-trade-amount 50
```

## Result
**No look-ahead bias detected** (98/100 signals checked, 0 biased). See
`VALIDATION_REPORT.md` for full output and honest caveats.

## Keeping this (optional)
Per the task, this is not committed to `stock-scanner` unless we decide to keep
it. If we do: copy `user_data/strategies/TjrValidation.py`, `PORTING_NOTES.md`,
`VALIDATION_REPORT.md`, and this README into `stock-scanner/validation/freqtrade/`
(NOT the venv or downloaded data) with its own README, then commit & push.
```bash
# .gitignore for validation/freqtrade/: .venv/  user_data/data/  *.feather
```
