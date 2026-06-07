# L2 Absorption Reversal Strategy

Long-only intraday strategy for IBKR/TWS paper trading first. The strategy looks for heavy aggressive selling that fails to move price lower because bid liquidity replenishes, then waits for seller exhaustion and an upside trigger before entering.

The implementation is deliberately conservative. Missing trades is acceptable; entering during absorption is not.

## Layout

- `depth_book.py` handles IBKR market depth operation codes: insert, update, delete.
- `tape.py` tracks prints and infers buy/sell side from bid/ask, with tick-rule fallback.
- `features.py` computes rolling book/tape features and explainable scores.
- `signal_engine.py` enforces the phases: selling pressure, absorption, exhaustion, trigger.
- `risk_manager.py` rejects unsafe trades and sizes positions.
- `execution_manager.py` owns stale entry cancellation, partial-fill protection, duplicate bracket prevention, max-hold flattening, and kill switch handling.
- `live_engine.py` subscribes each configured symbol, maintains one book/tape per symbol, evaluates signals on the configured interval, and routes approved trades to IBKR.
- `replay.py` and `backtest.py` use recorded logs and the same signal/risk logic.

## Commands

```powershell
python -m absorption.main paper --config absorption/config.sample.json
python -m absorption.main live --config absorption/config.sample.json
python -m absorption.main backtest --config absorption/config.sample.json --data data/
python -m absorption.main replay --run-dir runs/YYYYMMDD_HHMMSS
```

## Safety Defaults

- Long only.
- Paper trading config by default, IBKR port `7497`.
- Trading window: 9:30-11:30 AM America/New_York.
- Max hold: 30 minutes.
- One active trade per symbol.
- Max 3 trades per day.
- Risk per trade: 0.25% of $50,000.
- Bracket protection is created only after fills and only for filled quantity.

## Notes

IBKR market depth is not guaranteed to show every quote. Treat absorption scores as a conservative filter, not proof of hidden liquidity. Live order placement should be enabled only after paper replay and reconciliation behavior are reviewed.
