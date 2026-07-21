from backend.app.audio import AudioProcessingError, calculate_audio_part_specs


def test_audio_at_or_below_15_minutes_is_registered_as_single_part():
    parts = calculate_audio_part_specs(15 * 60)

    assert parts == [(0.0, 900.0, 0.0)]


def test_audio_over_15_and_at_or_below_30_minutes_is_split_in_half_without_overlap():
    parts = calculate_audio_part_specs(16 * 60)

    assert parts == [
        (0.0, 480.0, 0.0),
        (480.0, 960.0, 0.0),
    ]


def test_audio_at_30_minutes_is_split_in_half_without_overlap():
    parts = calculate_audio_part_specs(30 * 60)

    assert parts == [
        (0.0, 900.0, 0.0),
        (900.0, 1800.0, 0.0),
    ]


def test_audio_over_30_minutes_uses_15_minute_chunks_with_half_minute_overlap():
    parts = calculate_audio_part_specs(44 * 60)

    assert parts == [
        (0.0, 900.0, 0.0),
        (870.0, 1770.0, 30.0),
        (1740.0, 2640.0, 30.0),
    ]


def test_audio_part_specs_are_sorted_by_start_time():
    parts = calculate_audio_part_specs(46 * 60)

    assert parts == sorted(parts, key=lambda item: item[0])


def test_overlap_must_be_shorter_than_chunk_duration():
    try:
        calculate_audio_part_specs(
            31 * 60,
            target_chunk_minutes=15,
            chunk_overlap_minutes=15,
        )
    except AudioProcessingError as exc:
        assert exc.code == "audio_split_invalid_config"
    else:
        raise AssertionError("Expected AudioProcessingError")
