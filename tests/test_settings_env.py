import pytest

from fenrir.settings import mask_secret, parse_env_text, update_env_text


def test_update_replaces_existing_key_in_place():
    text = "A=1\nFENRIR_NT_TOKEN=old\nB=2\n"
    assert update_env_text(text, {"FENRIR_NT_TOKEN": "new"}) == "A=1\nFENRIR_NT_TOKEN=new\nB=2\n"


def test_update_appends_missing_key():
    assert update_env_text("A=1", {"B": "2"}) == "A=1\nB=2\n"
    assert update_env_text("", {"B": "2"}) == "B=2\n"


def test_update_keeps_comments_blank_and_unknown_lines_byte_for_byte():
    text = (
        "# LSE key\r\n\n  export  FENRIR_LSE_API_KEY = 'old'  # note\r\nnot a pair\nOTHER=x # y\n"
    )
    assert update_env_text(text, {"FENRIR_LSE_API_KEY": "new"}) == (
        "# LSE key\r\n\nFENRIR_LSE_API_KEY=new\r\nnot a pair\nOTHER=x # y\n"
    )


@pytest.mark.parametrize(
    "value", ["", "a=b", "a#b", "x # y", " padded ", "'quoted'", '"dq"', "it's", "#lead"]
)
def test_update_round_trips_through_parse(value):
    assert parse_env_text(update_env_text("# c\n", {"K": value})) == {"K": value}


def test_update_rejects_multiline_value():
    with pytest.raises(ValueError, match="K"):
        update_env_text("", {"K": "a\nINJECTED=1"})


def test_mask_shows_last_four_of_long_secret():
    assert mask_secret("sk-abcdefgh1234") == "••••1234"


@pytest.mark.parametrize("value", ["a", "abcd", "abcdefghijk"])
def test_mask_hides_short_secret_entirely(value):
    assert mask_secret(value) == "••••"


def test_mask_empty_secret_stays_empty():
    assert mask_secret("") == ""
