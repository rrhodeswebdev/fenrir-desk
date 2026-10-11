import os
import shutil
from datetime import date
from pathlib import Path

import pytest

from fenrir.rules.loader import list_accounts, load_account, rules_path
from fenrir.rules.schema import AccountRules
from fenrir.rules.writer import changes, write_account

RULES = Path(__file__).parents[2] / "rules"
IDS = list_accounts(RULES)


@pytest.fixture
def rules_dir(tmp_path) -> Path:
    shutil.copytree(RULES, tmp_path / "rules")
    return tmp_path / "rules"


def edit(rules: AccountRules, path: str, value) -> AccountRules:
    """`rules` with the value at dotted `path` replaced; skips validation, like a bad form would."""
    head, _, rest = path.partition(".")
    if not rest:
        return rules.model_copy(update={head: value})
    return rules.model_copy(update={head: edit(getattr(rules, head), rest, value)})


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    assert len(a) == len(b)
    return [(x, y) for x, y in zip(a, b, strict=True) if x != y]


def test_changes_compares_by_value():
    old = {"a": 2100, "b": {"c": 1, "d": [1, 2]}, "e": None}
    new = {"a": 2100.0, "b": {"c": 2, "d": (1, 3)}, "e": {"f": 1}}
    assert changes(old, new) == [(("b", "c"), 2), (("b", "d", 1), 3), (("e",), {"f": 1})]


def test_changes_replaces_a_list_that_changed_length():
    assert changes({"s": [1]}, {"s": [1, 2]}) == [(("s",), [1, 2])]


@pytest.mark.parametrize("aid", IDS)
def test_unchanged_save_is_byte_identical(rules_dir, aid):
    path = rules_path(aid, rules_dir)
    before = path.read_bytes()
    write_account(path, load_account(aid, rules_dir))
    assert path.read_bytes() == before


@pytest.mark.parametrize("aid", IDS)
def test_one_change_touches_only_its_line(rules_dir, aid):
    path = rules_path(aid, rules_dir)
    before = path.read_text()
    rules = edit(load_account(aid, rules_dir), "verified_on", date(2027, 1, 2))
    write_account(path, rules)
    assert [b for _, b in changed_lines(before, path.read_text())] == ["verified_on: 2027-01-02"]
    assert load_account(aid, rules_dir) == rules


def test_change_keeps_the_comment_on_its_line(rules_dir):
    path = rules_path("lucid-pro-50k", rules_dir)
    before = path.read_text()
    write_account(
        path,
        edit(load_account("lucid-pro-50k", rules_dir), "funded.payout.max_pct_of_profit", 20.5),
    )
    assert changed_lines(before, path.read_text()) == [
        (
            "    max_pct_of_profit: 19.23  # stands in for the $2,100 buffer, see above",
            "    max_pct_of_profit: 20.5   # stands in for the $2,100 buffer, see above",
        )
    ]
    assert path.read_text().startswith("# From Lucid's help center")


def test_flat_by_stays_quoted(rules_dir):
    path = rules_path("lucid-flex-50k", rules_dir)
    before = path.read_text()
    write_account(path, edit(load_account("lucid-flex-50k", rules_dir), "session.flat_by", "16:40"))
    assert [b for _, b in changed_lines(before, path.read_text())] == [
        'session: { flat_by: "16:40", tz: America/New_York }'
    ]


def test_whole_number_is_written_as_int(rules_dir):
    path = rules_path("lucid-flex-50k", rules_dir)
    before = path.read_text()
    rules = load_account("lucid-flex-50k", rules_dir)
    rules = edit(rules, "evaluation.profit_target", 3500.0)
    rules = edit(
        rules,
        "evaluation.max_drawdown",
        rules.evaluation.max_drawdown.model_copy(update={"locks_at": 2100.0}),
    )
    write_account(path, rules)
    assert [b for _, b in changed_lines(before, path.read_text())] == ["  profit_target: 3500"]


def test_null_to_object_round_trips(rules_dir):
    path = rules_path("lucid-flex-50k", rules_dir)
    rules = load_account("lucid-flex-50k", rules_dir)
    rules = edit(rules, "funded.consistency", rules.evaluation.consistency)
    write_account(path, rules)
    assert load_account("lucid-flex-50k", rules_dir) == rules
    assert "  consistency: { max_day_pct: 50 }\n" in path.read_text().split("funded:")[1]


def test_added_tier_keeps_the_flow_style(rules_dir):
    path = rules_path("lucid-flex-50k", rules_dir)
    rules = load_account("lucid-flex-50k", rules_dir)
    tiers = (
        *rules.funded.scaling,
        rules.funded.scaling[-1].model_copy(update={"from_profit": 3000.0, "max_minis": 5}),
    )
    rules = edit(edit(rules, "funded.scaling", tiers), "contracts.max_minis", 5)
    write_account(path, rules)
    assert load_account("lucid-flex-50k", rules_dir) == rules
    assert "    - { from_profit: 3000, max_minis: 5 }\nsession:" in path.read_text()


@pytest.mark.parametrize(
    ("field", "value"),
    [("funded.max_drawdown.amount", -5), ("account", "flex-100k"), ("session.flat_by", "25:00")],
)
def test_invalid_result_writes_nothing(rules_dir, field, value):
    path = rules_path("lucid-flex-50k", rules_dir)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="can't be saved as YAML: .*flex-50k.yaml"):
        write_account(path, edit(load_account("lucid-flex-50k", rules_dir), field, value))
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == sorted(
        p.name for p in (RULES / "lucid").iterdir()
    )


def test_write_is_an_atomic_replace(rules_dir, monkeypatch):
    path = rules_path("lucid-flex-50k", rules_dir)
    before = path.read_bytes()

    def crash(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        write_account(
            path, edit(load_account("lucid-flex-50k", rules_dir), "verified_on", date(2027, 1, 2))
        )
    assert path.read_bytes() == before
    assert not [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_write_keeps_the_file_mode(rules_dir):
    path = rules_path("lucid-flex-50k", rules_dir)
    path.chmod(0o640)
    write_account(
        path, edit(load_account("lucid-flex-50k", rules_dir), "verified_on", date(2027, 1, 2))
    )
    assert path.stat().st_mode & 0o777 == 0o640


def test_root_keeps_the_file_owner(rules_dir, monkeypatch):
    path = rules_path("lucid-flex-50k", rules_dir)
    st = path.stat()
    chowned = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda p, uid, gid: chowned.append((Path(p).name, uid, gid)))
    write_account(
        path, edit(load_account("lucid-flex-50k", rules_dir), "verified_on", date(2027, 1, 2))
    )
    assert chowned == [(".flex-50k.yaml.tmp", st.st_uid, st.st_gid)]


def test_save_works_without_geteuid(rules_dir, monkeypatch):  # native Windows
    monkeypatch.delattr(os, "geteuid", raising=False)
    path = rules_path("lucid-flex-50k", rules_dir)
    write_account(
        path, edit(load_account("lucid-flex-50k", rules_dir), "verified_on", date(2027, 1, 2))
    )
    assert "2027-01-02" in path.read_text()
