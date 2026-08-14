from weather_alpha.collectors.polymarket.parser import (
    TARGET_CITIES,
    parse_gamma_market,
    parse_temperature_bucket,
)


def test_target_cities_are_the_six_research_cities() -> None:
    assert (
        frozenset({"paris", "london", "munich", "amsterdam", "new york", "milan"}) == TARGET_CITIES
    )


def test_parse_exact_and_tail_buckets() -> None:
    exact = parse_temperature_bucket("31°C")
    assert exact is not None
    assert exact.kind == "exact"
    assert exact.min_c == 31.0
    assert exact.max_c == 31.0

    below = parse_temperature_bucket("30°C or below")
    assert below is not None
    assert below.kind == "below"
    assert below.max_c == 30.0
    assert below.min_c is None

    above = parse_temperature_bucket("40°C or higher")
    assert above is not None
    assert above.kind == "above"
    assert above.min_c == 40.0
    assert above.max_c is None


def test_negative_celsius_exact_below_above_and_range_buckets() -> None:
    exact = parse_temperature_bucket("-2°C")
    assert exact is not None
    assert exact.kind == "exact"
    assert exact.min_c == -2.0
    assert exact.max_c == -2.0

    below = parse_temperature_bucket("-1°C or below")
    assert below is not None
    assert below.kind == "below"
    assert below.max_c == -1.0
    assert below.min_c is None

    above = parse_temperature_bucket("-2°C or above")
    assert above is not None
    assert above.kind == "above"
    assert above.min_c == -2.0
    assert above.max_c is None

    ranged = parse_temperature_bucket("-2–-1°C")
    assert ranged is not None
    assert ranged.kind == "range"
    assert ranged.min_c == -2.0
    assert ranged.max_c == -1.0


def test_unrecognized_bucket_is_none_not_invented() -> None:
    assert parse_temperature_bucket("sunny and warm") is None
    assert parse_temperature_bucket("") is None
    assert parse_temperature_bucket("around -2 degrees") is None


def test_parse_gamma_market_extracts_city_station_and_date() -> None:
    payload = {
        "id": "123",
        "question": "Highest temperature in Paris on July 15?",
        "conditionId": "0x" + "ab" * 32,
        "slug": "highest-temperature-in-paris-on-july-15-2026",
        "description": (
            "This market will resolve to the temperature range that contains the "
            "highest temperature recorded at the Paris-Le Bourget Airport Station "
            "in degrees Celsius on 15 Jul '26. Resolution source: "
            "https://www.wunderground.com/history/daily/fr/bonneuil-en-france/LFPB"
        ),
        "groupItemTitle": "31°C",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["token-yes", "token-no"]',
        "closed": False,
        "active": True,
        "startDate": "2026-07-14T00:00:00Z",
        "endDate": "2026-07-16T00:00:00Z",
        "events": [{"id": "99"}],
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.city == "paris"
    assert parsed.market.station_icao == "LFPB"
    assert parsed.market.event_date == "2026-07-15"
    assert parsed.market.parse_status == "resolved"
    assert parsed.outcomes[0].bucket_kind == "exact"
    assert parsed.outcomes[0].temperature_celsius_min == 31.0


def test_unresolved_question_is_retained_without_invented_metadata() -> None:
    payload = {
        "id": "9",
        "question": "Will it rain somewhere tomorrow?",
        "conditionId": "0x" + "cd" * 32,
        "description": "No station listed.",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["a", "b"]',
    }
    parsed = parse_gamma_market(payload, retrieved_url="https://gamma-api.polymarket.com/markets")
    assert parsed.market.question == "Will it rain somewhere tomorrow?"
    assert parsed.market.city is None
    assert parsed.market.station_icao is None
    assert parsed.market.event_date is None
    assert parsed.market.parse_status == "unresolved"
    assert parsed.outcomes[0].bucket_kind is None


def _date_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "123",
        "question": "Highest temperature in Paris on July 15?",
        "conditionId": "0x" + "ab" * 32,
        "slug": "highest-temperature-in-paris-on-july-15-2026",
        "description": "Station LFPB.",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["token-yes", "token-no"]',
    }
    payload.update(overrides)
    return payload


def test_incomplete_question_does_not_mix_conflicting_slug_month_day() -> None:
    parsed = parse_gamma_market(
        _date_payload(slug="highest-temperature-in-paris-on-august-16-2026"),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    assert any("conflict" in note.lower() for note in parsed.market.parse_notes)


def test_complete_question_date_conflicts_with_slug_and_stays_unresolved() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15, 2026?",
            slug="highest-temperature-in-paris-on-august-16-2026",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    assert any("conflict" in note.lower() for note in parsed.market.parse_notes)


def test_incomplete_question_agrees_with_complete_slug_date() -> None:
    parsed = parse_gamma_market(
        _date_payload(description="No year here."),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date == "2026-07-15"


def test_ambiguous_description_years_do_not_fill_missing_year() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            slug="highest-temperature-in-paris-on-july-15",
            description="Compared 2025 and 2026 seasons at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes).lower()
    assert "year" in notes
    assert "2026-07-15" not in notes


def test_description_august_16_does_not_lend_year_to_july_15_question_and_slug() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15?",
            slug="highest-temperature-in-paris-on-july-15",
            description="This market resolves on August 16, 2026 at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes).lower()
    assert "conflict" in notes
    assert "question" in notes
    assert "slug" in notes
    assert "description" in notes
    assert "2026-08-16" in notes
    assert "07-15" in notes


def test_matching_description_complete_date_supplies_july_15_2026() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15?",
            slug="highest-temperature-in-paris-on-july-15",
            description="Highest temperature recorded on 15 Jul '26 at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date == "2026-07-15"


def test_structured_event_date_not_start_or_end_date() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris?",
            slug="highest-temperature-in-paris",
            description="No calendar date in this text.",
            eventDate="2026-07-15",
            startDate="2026-08-16T00:00:00Z",
            endDate="2026-08-17T00:00:00Z",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date == "2026-07-15"


def test_structured_event_date_conflicts_with_question_and_stays_unresolved() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15, 2026?",
            eventDate="2026-08-16",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes).lower()
    assert "conflict" in notes
    assert "eventdate" in notes
    assert "structured=" not in notes
    assert "question" in notes
    assert "2026-07-15" in notes
    assert "2026-08-16" in notes


def test_description_only_complete_date_does_not_establish_event_date() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris?",
            slug="highest-temperature-in-paris",
            description="This market resolves on July 15, 2026 at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes).lower()
    assert "description" in notes
    assert "2026-07-15" in notes
    assert "not sufficient" in notes


def test_description_validates_matching_question_slug_date() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15?",
            slug="highest-temperature-in-paris-on-july-15",
            description="Resolves using the July 15, 2026 observation at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date == "2026-07-15"


def test_description_conflicts_with_question_slug_date() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris on July 15?",
            slug="highest-temperature-in-paris-on-july-15",
            description="This market resolves on August 16, 2026 at LFPB.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes).lower()
    assert "question" in notes
    assert "slug" in notes
    assert "description" in notes
    assert "07-15" in notes
    assert "2026-08-16" in notes


def test_conflicting_structured_eventdate_and_date_fields_stay_unresolved() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris?",
            slug="highest-temperature-in-paris",
            description="No calendar date in this text.",
            eventDate="2026-07-15",
            date="2026-08-16",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
    notes = " ".join(parsed.market.parse_notes)
    assert "eventDate=2026-07-15" in notes
    assert "date=2026-08-16" in notes
    assert "structured=" not in notes


def test_matching_structured_eventdate_and_date_fields_resolve() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris?",
            slug="highest-temperature-in-paris",
            description="No calendar date in this text.",
            eventDate="2026-07-15",
            date="2026-07-15",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date == "2026-07-15"


def test_all_date_evidence_missing_stays_none() -> None:
    parsed = parse_gamma_market(
        _date_payload(
            question="Highest temperature in Paris?",
            slug="highest-temperature-in-paris",
            description="No calendar date in this text.",
        ),
        retrieved_url="https://gamma-api.polymarket.com/markets",
    )
    assert parsed.market.event_date is None
