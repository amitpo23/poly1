# Brain Exit Authority

Date: 2026-05-21

Purpose: give the brain more authority over profitable holds and ordinary
timeouts, without letting it override hard money-safety controls.

## Principle

The brain manages profit capture and continuation.

Technical guardrails stop catastrophic or unmanaged states.

## What the brain may override

- Regular `take_profit` decisions below the hard profit cap.
- Ordinary `timeout` exits, only inside a bounded extension window.
- Early profit-taking when the LLM/brain says the same position is still worth
  entering at the current price.

## What the brain may not override

- `HALT`.
- Missing/stale `position_manager` or supervisor safety.
- Hard stop-loss.
- Hard profit cap.
- Absolute max-hold extension.
- Minimum notional and exit-liquidity checks.
- Drawdown/equity guards.

## Runtime controls

| Variable | Default | Meaning |
|---|---:|---|
| `MAINTAIN_BRAIN_EXIT_AUTHORITY_ENABLED` | `true` | Enables brain override of regular TP/timeout exits. |
| `MAINTAIN_BRAIN_HOLD_OVERRIDE_CONFIDENCE` | `0.65` | Minimum confidence for holding through a regular profit exit. |
| `MAINTAIN_BRAIN_EXTEND_HOLD_CONFIDENCE` | `0.75` | Minimum confidence for extending an ordinary timeout. |
| `MAINTAIN_BRAIN_MAX_HOLD_EXTENSION_HOURS` | `1.0` | Extra bounded time beyond `MAINTAIN_MAX_HOLD_HOURS`. |

## Exit decision format

The LLM/brain exit review may return:

```json
{
  "action": "HOLD | EXTEND_HOLD | TAKE_PROFIT | EXIT_NOW | TIGHTEN_STOP",
  "reason": "one sentence",
  "confidence": 0.0,
  "target_exit_price": null,
  "max_hold_seconds": null
}
```

`HOLD` and `EXTEND_HOLD` can suppress a regular `take_profit` / `timeout` close
only if confidence clears the configured threshold and the position is still
inside the hard guardrails.

## FAK no-match handling

`FAK` sell failures with `no orders found to match` are now treated as
`exit_deferred`, not `close_failed`.

Rationale: no-match is usually transient exit liquidity or limit-price friction,
not proof that the market resolved against us. The position stays open and
continues to be managed on the next cycle. Real errors still write
`close_failed`; resolved/delisted markets still write `resolved_loss`.

## Audit trail

Every brain hold override writes a `brain_decisions` row:

- `agent=position_manager`
- `strategy=brain_exit_authority`
- `decision_type=exit`
- `reason=brain_hold_override`
- `action=HOLD`

Every no-match deferral writes:

- trade row status `exit_deferred`
- `brain_decisions` row with `strategy=execution_quality`,
  `reason=exit_not_matched_deferred`

