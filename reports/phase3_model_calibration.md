# Phase 3 model calibration

## MEASURED DATA
- chronological unique event_date split 60/20/20; train=['2026-03-20', '2026-03-21', '2026-03-22', '2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29', '2026-03-30', '2026-03-31', '2026-04-01', '2026-04-02', '2026-04-03', '2026-04-04', '2026-04-05', '2026-04-06']; validation=['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-11', '2026-04-12']; test=['2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17', '2026-04-18']
- snapshots train=946 validation=396 test=396
- validation scored_events=36 skipped=0 reasons=[]; test scored_events=36 skipped=0 reasons=[]
- requested_dates=2026-03-20..2026-04-18
- usable_event_dates=['2026-03-20', '2026-03-21', '2026-03-22', '2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29', '2026-03-30', '2026-03-31', '2026-04-01', '2026-04-02', '2026-04-03', '2026-04-04', '2026-04-05', '2026-04-06', '2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-11', '2026-04-12', '2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17', '2026-04-18']
- providers=['Polymarket Gamma public-search', 'Polymarket CLOB GET /prices-history', 'Open-Meteo Single Runs (ecmwf_ifs)', 'Open-Meteo Archive (diagnostic only)']

## MODEL OUTPUT
- validation={"baseline":{"clipped_log_loss":25.321679238961675,"ece":0.1356902356902357,"multiclass_brier":1.3812612233445571,"name":"HistoricalFrequencyBaseline","probability_sum_ok":true},"bucket_coverage":["10\u00b0C","10\u00b0C or below","11\u00b0C","12\u00b0C","12\u00b0C or below","13\u00b0C","13\u00b0C or below","14\u00b0C","14\u00b0C or below","15\u00b0C","15\u00b0C or below","15\u00b0C or higher","16\u00b0C","16\u00b0C or below","16\u00b0C or higher","17\u00b0C","17\u00b0C or below","17\u00b0C or higher","18\u00b0C","18\u00b0C or below","18\u00b0C or higher","19\u00b0C","20\u00b0C","20\u00b0C or higher","21\u00b0C","22\u00b0C","22\u00b0C or higher","23\u00b0C","23\u00b0C or higher","24\u00b0C","24\u00b0C or higher","25\u00b0C","25\u00b0C or higher","26\u00b0C","26\u00b0C or higher","27\u00b0C","27\u00b0C or higher","28\u00b0C or higher","37\u00b0F or below","38-39\u00b0F","40-41\u00b0F","41\u00b0F or below","42-43\u00b0F","44-45\u00b0F","45\u00b0F or below","46-47\u00b0F","47\u00b0F or below","48-49\u00b0F","50-51\u00b0F","52-53\u00b0F","54-55\u00b0F","55\u00b0F or below","56-57\u00b0F","56\u00b0F or higher","58-59\u00b0F","59\u00b0F or below","5\u00b0C or below","60-61\u00b0F","60\u00b0F or higher","62-63\u00b0F","64-65\u00b0F","64\u00b0F or higher","66-67\u00b0F","66\u00b0F or higher","68-69\u00b0F","6\u00b0C","6\u00b0C or below","70-71\u00b0F","72-73\u00b0F","74-75\u00b0F","74\u00b0F or higher","76-77\u00b0F","78\u00b0F or higher","7\u00b0C","7\u00b0C or below","8\u00b0C","8\u00b0C or below","9\u00b0C"],"clipped_log_loss":11.332487315705656,"ece":0.07737877961808443,"events":36,"multiclass_brier":0.8874619251545255,"n":396,"probability_sum_ok":true,"scored_events":36,"skip_reasons":[],"skipped_events":0,"status":"ok"}
- test={"baseline":{"clipped_log_loss":25.25686377363777,"ece":0.1313131313131313,"multiclass_brier":1.2532000561167231,"name":"HistoricalFrequencyBaseline","probability_sum_ok":true},"bucket_coverage":["10\u00b0C","10\u00b0C or below","11\u00b0C","11\u00b0C or below","11\u00b0C or higher","12\u00b0C","12\u00b0C or below","13\u00b0C","13\u00b0C or below","14\u00b0C","14\u00b0C or below","15\u00b0C","15\u00b0C or higher","16\u00b0C","16\u00b0C or below","16\u00b0C or higher","17\u00b0C","17\u00b0C or higher","18\u00b0C","18\u00b0C or below","18\u00b0C or higher","19\u00b0C","19\u00b0C or higher","1\u00b0C or below","20\u00b0C","20\u00b0C or higher","21\u00b0C","21\u00b0C or higher","22\u00b0C","22\u00b0C or higher","23\u00b0C","23\u00b0C or higher","24\u00b0C","24\u00b0C or higher","25\u00b0C","26\u00b0C","26\u00b0C or higher","27\u00b0C","28\u00b0C or higher","2\u00b0C","3\u00b0C","4\u00b0C","53\u00b0F or below","54-55\u00b0F","56-57\u00b0F","58-59\u00b0F","5\u00b0C","5\u00b0C or below","60-61\u00b0F","61\u00b0F or below","62-63\u00b0F","64-65\u00b0F","66-67\u00b0F","68-69\u00b0F","6\u00b0C","6\u00b0C or below","70-71\u00b0F","71\u00b0F or below","72-73\u00b0F","72\u00b0F or higher","74-75\u00b0F","76-77\u00b0F","77\u00b0F or below","78-79\u00b0F","79\u00b0F or below","7\u00b0C","7\u00b0C or below","80-81\u00b0F","80\u00b0F or higher","82-83\u00b0F","84-85\u00b0F","86-87\u00b0F","88-89\u00b0F","8\u00b0C","8\u00b0C or below","90-91\u00b0F","90\u00b0F or higher","92-93\u00b0F","94-95\u00b0F","96-97\u00b0F","96\u00b0F or higher","98\u00b0F or higher","9\u00b0C","9\u00b0C or below"],"clipped_log_loss":9.72521624489927,"ece":0.08301230731043567,"events":36,"multiclass_brier":0.9876350901452563,"n":396,"probability_sum_ok":true,"scored_events":36,"skip_reasons":[],"skipped_events":0,"status":"ok"}
- HistoricalFrequencyBaseline={"clipped_log_loss":25.25686377363777,"ece":0.1313131313131313,"multiclass_brier":1.2532000561167231,"name":"HistoricalFrequencyBaseline","probability_sum_ok":true}
- sample_assessment={"conclusion":"descriptive_only","operational_minimum_scored_events":30,"reason":"scored events 36 meet the operational minimum of 30 (assumption, not a universal statistical threshold). Results remain descriptive; no profitability is claimed.","scored_events":36,"status":"meets_operational_minimum"}

## ASSUMPTIONS
- Forecast-error distribution and frequency baseline are fit on train dates only.
- Settlement labels are evaluation targets, not features.
- Validation/test labels are not used during fitting.
- Operational minimum unique scored events=30 (pipeline assumption, not a universal statistical threshold).

## MISSING DATA
- If n is small, Brier/log loss/ECE are descriptive only.
- Historical asks are absent. Calibration metrics use Gamma settlement labels; market-mispricing comparisons use descriptive CLOB p, and neither implies fills.
- scored events 36 meet the operational minimum of 30 (assumption, not a universal statistical threshold). Results remain descriptive; no profitability is claimed.

## INFERENCES
- No alpha claim is made from calibration scores alone.
- Sample conclusion: descriptive_only.
- Insufficient executable sample: do not treat scores as tradable edge.

## LIMITATIONS
- Providers: Polymarket Gamma public-search, CLOB GET /prices-history, Open-Meteo Single Runs (https://open-meteo.com/en/docs/single-runs-api), Open-Meteo Archive. Previous Runs docs: https://open-meteo.com/en/docs/previous-runs-api.
- CLOB prices-history p is descriptive market_probability only; historical asks are unavailable.
- Open-Meteo Archive maxima are diagnostic grid/reanalysis values, not Wunderground settlement.
- Open-Meteo Archive is retrospective and is not a decision-time observation feature.
- Gamma public-search is a current search index, not a guaranteed archival universe; delisted, unindexed, or metadata-mutated markets may be absent, so survivorship bias is possible and market counts must not be read as complete historical coverage.
- Fees, slippage, and leverage are not modeled. No compounding or Kelly sizing.
- No alpha is claimed when the executable sample is insufficient.
