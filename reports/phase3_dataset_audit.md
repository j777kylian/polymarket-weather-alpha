# Phase 3 dataset audit

## MEASURED DATA
- markets=1738
- snapshots=1738
- providers=['Polymarket Gamma public-search', 'Polymarket CLOB GET /prices-history', 'Open-Meteo Single Runs (ecmwf_ifs)', 'Open-Meteo Archive (diagnostic only)']
- requested_dates=2026-03-20..2026-04-18
- usable_event_dates=['2026-03-20', '2026-03-21', '2026-03-22', '2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29', '2026-03-30', '2026-03-31', '2026-04-01', '2026-04-02', '2026-04-03', '2026-04-04', '2026-04-05', '2026-04-06', '2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-11', '2026-04-12', '2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17', '2026-04-18']
- max_search_pages=10 search_limit_per_type=50 cities=['amsterdam', 'london', 'milan', 'munich', 'new york', 'paris']
- discovered_outside_range=13240
- price_http_errors=0 price_history_empty=26
- city_coverage={"amsterdam":176,"london":308,"milan":319,"munich":308,"new york":319,"paris":308}
- station_coverage={"EDDM":308,"EGLC":308,"EHAM":176,"KLGA":319,"LFPG":308,"LIMC":319}
- date_coverage={"2026-03-20":11,"2026-03-21":55,"2026-03-22":55,"2026-03-23":55,"2026-03-24":55,"2026-03-25":55,"2026-03-26":55,"2026-03-27":55,"2026-03-28":55,"2026-03-29":55,"2026-03-30":55,"2026-03-31":11,"2026-04-01":55,"2026-04-02":55,"2026-04-03":66,"2026-04-04":66,"2026-04-05":66,"2026-04-06":66,"2026-04-07":66,"2026-04-08":66,"2026-04-09":66,"2026-04-10":66,"2026-04-11":66,"2026-04-12":66,"2026-04-13":66,"2026-04-14":66,"2026-04-15":66,"2026-04-16":66,"2026-04-17":66,"2026-04-18":66}
- duplicates=0
- exclusions={"not a temperature market":252,"unknown/ambiguous event date":6}

## MODEL OUTPUT
- Dataset audit does not fit a probability model.

## ASSUMPTIONS
- chronological unique event_date split 60/20/20; train=['2026-03-20', '2026-03-21', '2026-03-22', '2026-03-23', '2026-03-24', '2026-03-25', '2026-03-26', '2026-03-27', '2026-03-28', '2026-03-29', '2026-03-30', '2026-03-31', '2026-04-01', '2026-04-02', '2026-04-03', '2026-04-04', '2026-04-05', '2026-04-06']; validation=['2026-04-07', '2026-04-08', '2026-04-09', '2026-04-10', '2026-04-11', '2026-04-12']; test=['2026-04-13', '2026-04-14', '2026-04-15', '2026-04-16', '2026-04-17', '2026-04-18']
- Timestamps are timezone-aware UTC.
- Date filters are applied only after conservative market parsing.
- Out-of-range discovered markets are counted, not listed per-market in quarantine.

## MISSING DATA
- field_missingness={"best_ask":1738,"cloud_cover_pct":0,"dew_point_c":0,"diagnostic_actual_max_c":0,"executable_entry_price":1738,"forecast_daily_max_c":0,"humidity_pct":0,"market_probability":26,"observation_max_so_far_c":1738,"precipitation":0,"settlement_label":0,"surface_pressure":0,"wind_speed":0}
- exclusions={"not a temperature market":252,"unknown/ambiguous event date":6}
- best_ask/executable_entry_price missingness is expected: historical books are unavailable.
- diagnostic_actual_max_c is Open-Meteo Archive, not settlement.
- Settlement labels are not decision-time features.
- Open-Meteo Archive hourly rows are not decision-time observation features.

## INFERENCES
- No trading inference is drawn from coverage counts.
- timestamp_violations=none

## LIMITATIONS
- Providers: Polymarket Gamma public-search, CLOB GET /prices-history, Open-Meteo Single Runs (https://open-meteo.com/en/docs/single-runs-api), Open-Meteo Archive. Previous Runs docs: https://open-meteo.com/en/docs/previous-runs-api.
- CLOB prices-history p is descriptive market_probability only; historical asks are unavailable.
- Open-Meteo Archive maxima are diagnostic grid/reanalysis values, not Wunderground settlement.
- Open-Meteo Archive is retrospective and is not a decision-time observation feature.
- Gamma public-search is a current search index, not a guaranteed archival universe; delisted, unindexed, or metadata-mutated markets may be absent, so survivorship bias is possible and market counts must not be read as complete historical coverage.
- Fees, slippage, and leverage are not modeled. No compounding or Kelly sizing.
- No alpha is claimed when the executable sample is insufficient.
- snapshots train=946 validation=396 test=396
- validation scored_events=36 skipped=0 reasons=[]; test scored_events=36 skipped=0 reasons=[]
