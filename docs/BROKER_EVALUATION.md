# Kamandal Broker Evaluation

**Date:** April 3, 2026
**Scope:** Retail broker/API options for a multi-leg options portfolio manager

## What Kamandal Needs

For Kamandal, the execution broker is not just a place to send orders. The broker/API needs to support:

- Multi-leg options order entry
- Reliable account, positions, balances, and order-state access
- Option chain and quote access suitable for live decision-making
- Reasonable costs for frequent premium-selling workflows
- Personal automation without fighting the broker's terms

## Current Recommendation

### Best default choice: tastytrade

Why:

- Most options-native API surface of the three candidates
- Clear support for complex orders, order dry runs, option chains, balances/positions, streaming market data, and streaming account updates
- Sandbox is available for development and resets automatically
- Strong fit for multi-leg premium-selling workflows and futures/options expansion later

Tradeoff:

- More expensive than Public on stock/ETF option execution because tastytrade charges opening commissions

### Best low-cost choice: Public

Why:

- Strong current Individual API with multi-leg options support
- Commission-free API with stock/ETF options rebates
- Real-time account data, quotes, option chains, and option greeks are exposed in the current docs

Tradeoff:

- The Individual API is explicitly for personal, non-commercial use
- Public reserves broad control over throttles and program changes
- Better fit if you want a personal trading automation stack, less ideal if Kamandal may evolve into a shared product or third-party platform

### Most uncertain choice right now: Schwab

Why:

- Strong trading platforms and broad support for multi-leg options at the platform level
- thinkorswim and Schwab tooling are deep

Tradeoff:

- As of April 3, 2026, Schwab's developer portal was returning a maintenance page, so I could not fully verify the current retail API surface for multi-leg options, option-chain data, and account-streaming features from official API docs
- Costs are materially higher than Public and generally higher than tastytrade for active stock/ETF options opening flow

## Comparison

| Broker | Multi-leg options via official materials | API/account/data surface | Cost profile | Fit for Kamandal |
|---|---|---|---|---|
| tastytrade | Yes | Strongest options-focused API surface | Moderate | Best overall |
| Public | Yes | Very strong for a personal-use API | Lowest | Best low-cost personal stack |
| Schwab | Platform support clearly yes; API details not fully verified today | Unclear due portal maintenance | Higher | Wait-and-verify |

## Notes by Broker

### tastytrade

- Official docs show API coverage for balances/positions, instruments, margin requirements, orders, complex orders, order dry runs, market data, option chain fetches, DXLink streaming market data, and streaming account data.
- Sandbox is documented, with separate API and websocket hosts.
- Sandbox quotes are 15 minutes delayed.
- Pricing currently lists stock and ETF options at `$1` per contract to open with a `$10 max per leg`, and `$0` to close. Broad-based index options are `$1` to open and `$0` to close. Futures options pricing is separate.

Implication for Kamandal:

- This is the best execution API if we optimize for options workflow completeness and future expansion into futures/options on futures.

### Public

- Public's API page currently advertises real-time balances, buying power, positions, money movement history, open-order updates, real-time pricing data, option chains, single-leg options orders, and multi-leg options strategies.
- Public's option details docs expose option greeks.
- Public's Individual API agreement explicitly limits the program to personal, non-commercial use.
- Public can apply request throttles at the application or brokerage-account level and may change them without notice.
- Public's fee schedule currently says enrolled users can receive a rebate of `$0.06-$0.18` per stock or ETF option contract, with rebate details depending in part on whether the trade was placed via API.

Implication for Kamandal:

- If Kamandal is strictly your own automation system, Public is very attractive on cost.
- If Kamandal may later become a multi-user product, shared service, or externally distributed tool, Public's program terms are a strategic constraint.

### Schwab

- Schwab's official options pages clearly show support for placing single and multi-leg options orders across its platforms, plus platform features such as simulated trade and portfolio risk analysis.
- Schwab's pricing guide currently lists `$0.65` per options contract online, with buy-to-close fees waived for online trades priced at `$0.05` or less.
- On April 3, 2026, the Schwab developer portal search result was returning a maintenance notice, which blocked clean verification of the current API docs from official sources during this pass.

Implication for Kamandal:

- Schwab may still be viable, but it is not the best first integration choice until we can verify the live official API docs for retail options automation.

## Recommendation for Build Order

1. Start with tastytrade if the goal is the best execution/integration surface for multi-leg options automation.
2. Consider Public instead if minimizing recurring execution cost matters more than future commercialization flexibility.
3. Revisit Schwab after its official developer portal is accessible and we can verify the exact API capabilities.

## Sources

- [tastytrade pricing](https://tastytrade.com/pricing)
- [tastytrade developer SDK and API surface](https://developer.tastytrade.com/sdk/)
- [tastytrade sandbox](https://developer.tastytrade.com/sandbox/)
- [tastytrade account streamer demo](https://developer.tastytrade.com/account-streamer-demo/)
- [Public API overview](https://public.com/api)
- [Public option greeks API docs](https://public.com/api/docs/resources/option-details/get-option-greeks_1)
- [Public Individual API Agreement](https://public.com/documents/individual-api-program)
- [Public API policy](https://public.com/disclosures/api-policy)
- [Public fee schedule](https://public.com/documents/fee-schedule/)
- [Schwab options overview](https://www.schwab.com/options/trade-options)
- [Schwab pricing guide](https://www.schwab.com/legal/schwab-pricing-guide-for-individual-investors)
- [Schwab developer portal](https://developer.schwab.com/?q=user)
