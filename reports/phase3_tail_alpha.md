# Phase 3 tail alpha

## MEASURED DATA
- <1c: n=186 settled_yes_count=1 yes_frac=0.005376344086021506 mean_model_p=0.005183955302551128 mean_raw_edge=0.0018936327219059642
- 1-3c: n=44 settled_yes_count=0 yes_frac=0.0 mean_model_p=0.02038770053475936 mean_raw_edge=0.002705882352941176
- 3-5c: n=26 settled_yes_count=2 yes_frac=0.07692307692307693 mean_model_p=0.04454185520361991 mean_raw_edge=0.0050418552036199105

## MODEL OUTPUT
- jackpot_concentration=None (null; not return concentration)
- max_band_settled_yes_share=0.6666666666666666
- executable_survival=None status=unknown_no_historical_asks
- Largest 1/3/5 removal robustness PnL is null without fills.

## ASSUMPTIONS
- Tail bands are descriptive market prices: <1c, 1-3c, 3-5c.
- Settled YES counts/fractions use Gamma settlement labels.
- max_band_settled_yes_share is a YES-count share across bands, not return concentration.

## MISSING DATA
- executable_survival=null status=unknown_no_historical_asks because historical asks are absent.
- Historical asks are unavailable; executable_survival is null (status=unknown_no_historical_asks).
- Largest 1/3/5 removal robustness returns null PnL because there are no fills.
- jackpot_concentration is null: max-band settled YES share is a count statistic, not return concentration.
- pnl/roi/max_drawdown/profit_factor remain null without executable fills.

## INFERENCES
- No tail-alpha claim is made without executable fills.
- OOS breakdown is descriptive only on the test dates.
- Sample conclusion: descriptive_only.

## LIMITATIONS
- Providers: Polymarket Gamma public-search, CLOB GET /prices-history, Open-Meteo Single Runs (https://open-meteo.com/en/docs/single-runs-api), Open-Meteo Archive. Previous Runs docs: https://open-meteo.com/en/docs/previous-runs-api.
- CLOB prices-history p is descriptive market_probability only; historical asks are unavailable.
- Open-Meteo Archive maxima are diagnostic grid/reanalysis values, not Wunderground settlement.
- Open-Meteo Archive is retrospective and is not a decision-time observation feature.
- Gamma public-search is a current search index, not a guaranteed archival universe; delisted, unindexed, or metadata-mutated markets may be absent, so survivorship bias is possible and market counts must not be read as complete historical coverage.
- Fees, slippage, and leverage are not modeled. No compounding or Kelly sizing.
- No alpha is claimed when the executable sample is insufficient.
- chronological unique event_date split 60/20/20; train=['2026-03-20', '2026-03-21', '2026-03-22', '2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29', '2026-03-30', '2026-03-31', '2026-04-01', '2026-04-02', '2026-04-03', '2026-04-04', '2026-04-05', '2026-04-06']; validation=['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-11', '2026-04-12']; test=['2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17', '2026-04-18']
- snapshots train=946 validation=396 test=396
