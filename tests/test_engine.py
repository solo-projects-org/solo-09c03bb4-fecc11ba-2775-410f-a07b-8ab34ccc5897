from carryover.engine import (
    BLANK,
    SAMPLE,
    BLANK_CLEAR,
    BLANK_EXCEEDED,
    BLANK_MISSING,
    CARRYOVER_RISK,
    CLEAR,
    HIGH,
    Injection,
    compute_results,
    natural_key,
)


def inj(no, kind=SAMPLE, area=10.0, revision=1):
    return Injection(no, kind, area, revision)


def statuses(results):
    return [(r.injection_no, r.status, r.trigger_injection) for r in results]


def test_high_then_risk_until_clean_blank():
    rows = compute_results(
        [
            inj("S1", area=1200),
            inj("S2", area=100),
            inj("B1", BLANK, 10),
            inj("S3", area=100),
        ],
        high_threshold=1000,
        blank_limit=50,
    )
    assert statuses(rows) == [
        ("S1", HIGH, None),
        ("S2", CARRYOVER_RISK, "S1"),
        ("B1", BLANK_CLEAR, None),
        ("S3", CLEAR, None),
    ]


def test_exceeded_and_missing_blank_do_not_clear():
    rows = compute_results(
        [
            inj("S1", area=1200),
            inj("B1", BLANK, 80),
            inj("S2", area=100),
            inj("B2", BLANK, None),
            inj("S3", area=100),
            inj("B3", BLANK, 50),
            inj("S4", area=100),
        ],
        high_threshold=1000,
        blank_limit=50,
    )
    assert statuses(rows) == [
        ("S1", HIGH, None),
        ("B1", BLANK_EXCEEDED, "S1"),
        ("S2", CARRYOVER_RISK, "S1"),
        ("B2", BLANK_MISSING, "S1"),
        ("S3", CARRYOVER_RISK, "S1"),
        ("B3", BLANK_CLEAR, None),
        ("S4", CLEAR, None),
    ]


def test_later_high_sample_becomes_new_trigger():
    rows = compute_results(
        [
            inj("S1", area=1200),
            inj("S2", area=1300),
            inj("S3", area=100),
            inj("B1", BLANK, 10),
            inj("S4", area=100),
        ],
        high_threshold=1000,
        blank_limit=50,
    )
    by_no = {r.injection_no: r for r in rows}
    assert by_no["S2"].status == HIGH
    assert by_no["S3"].trigger_injection == "S2"
    assert by_no["S4"].status == CLEAR


def test_threshold_is_inclusive():
    rows = compute_results(
        [inj("S1", area=1000), inj("B1", BLANK, 50), inj("S2", area=100)],
        1000,
        50,
    )
    by_no = {r.injection_no: r for r in rows}
    assert by_no["S1"].status == HIGH
    assert by_no["B1"].status == BLANK_CLEAR
    assert by_no["S2"].status == CLEAR


def test_run_position_controls_order_not_injection_number():
    rows = compute_results(
        [
            Injection("S10", SAMPLE, 2000.0, 1, position=1),
            Injection("S2", SAMPLE, 10.0, 1, position=2),
            Injection("B1", BLANK, 10.0, 1, position=3),
            Injection("S3", SAMPLE, 10.0, 1, position=4),
        ],
        1000,
        50,
    )
    by_no = {r.injection_no: r for r in rows}
    assert by_no["S2"].status == CARRYOVER_RISK
    assert by_no["S2"].trigger_injection == "S10"
    assert by_no["S3"].status == CLEAR


def test_natural_key_is_available_for_display():
    values = ["S10", "S2", "S1", "B1"]
    assert sorted(values, key=lambda v: (natural_key(v), v)) == ["B1", "S1", "S2", "S10"]


def test_blank_before_high_does_not_clear_future_risk():
    rows = compute_results(
        [inj("B1", BLANK, 10), inj("S1", area=2000), inj("S2", area=10)],
        1000,
        50,
    )
    by_no = {r.injection_no: r for r in rows}
    assert by_no["S2"].status == CARRYOVER_RISK
    assert by_no["S2"].trigger_injection == "S1"
