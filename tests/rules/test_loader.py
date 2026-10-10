from pathlib import Path

import pytest
import yaml

from fenrir.rules.loader import account_id, dump_account, list_accounts, load_account, rules_path


def write(rules_dir: Path, data: dict, rel: str = "lucid/flex-50k.yaml") -> Path:
    path = rules_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_rules_path(tmp_path):
    assert rules_path("lucid-flex-50k", tmp_path) == tmp_path / "lucid" / "flex-50k.yaml"


@pytest.mark.parametrize("bad", ["lucid-../x", "../lucid-flex", "Lucid-Flex-50k", "lucid"])
def test_rules_path_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(ValueError, match="invalid account id"):
        rules_path(bad, tmp_path)


def test_load_account(tmp_path, base):
    write(tmp_path, base)
    rules = load_account("lucid-flex-50k", tmp_path)
    assert rules.contracts.max_minis == 4


def test_account_id(tmp_path, base):
    write(tmp_path, base)
    assert account_id(load_account("lucid-flex-50k", tmp_path)) == "lucid-flex-50k"


def test_unknown_account(tmp_path):
    with pytest.raises(ValueError, match="unknown account 'lucid-pro-50k'"):
        load_account("lucid-pro-50k", tmp_path)


def test_empty_file_names_path(tmp_path):
    path = tmp_path / "lucid" / "flex-50k.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    with pytest.raises(ValueError, match="flex-50k.yaml"):
        load_account("lucid-flex-50k", tmp_path)


def test_malformed_yaml_names_path(tmp_path):
    path = tmp_path / "lucid" / "flex-50k.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("firm: lucid\n\taccount: [flex")
    with pytest.raises(ValueError, match="flex-50k.yaml"):
        load_account("lucid-flex-50k", tmp_path)


@pytest.mark.parametrize(
    "old, new",
    [
        ("automation_allowed: true", "automation_allowed: true\nautomation_allowed: false"),
        ("  max_minis: 4", "  max_minis: 4\n  max_minis: 40"),
    ],
)
def test_duplicate_key_rejected(tmp_path, base, old, new):
    path = write(tmp_path, base)
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    with pytest.raises(ValueError, match=r"flex-50k\.yaml.*duplicate key"):
        load_account("lucid-flex-50k", tmp_path)


def test_merge_key_can_override(tmp_path, base):
    path = write(tmp_path, base)
    text = path.read_text()
    eval_dd = "evaluation:\n  profit_target: 3000\n  max_drawdown:"
    funded_dd = "funded:\n  max_drawdown:\n    amount: 2000\n"
    assert eval_dd in text and funded_dd in text
    merged = "funded:\n  max_drawdown:\n    <<: *dd\n    amount: 1500\n"
    path.write_text(text.replace(eval_dd, eval_dd + " &dd").replace(funded_dd, merged))
    rules = load_account("lucid-flex-50k", tmp_path)
    assert rules.funded.max_drawdown.amount == 1500
    assert rules.funded.max_drawdown.locks_at == 2100


def test_bad_date_names_path(tmp_path, base):
    path = write(tmp_path, base)
    path.write_text(path.read_text().replace("verified_on: null", "verified_on: 2026-13-01"))
    with pytest.raises(ValueError, match="flex-50k.yaml"):  # PyYAML raises a bare ValueError
        load_account("lucid-flex-50k", tmp_path)


def test_unreadable_path_is_value_error(tmp_path):
    (tmp_path / "lucid" / "flex-50k.yaml").mkdir(parents=True)  # a directory, not a file
    with pytest.raises(ValueError, match="flex-50k.yaml"):
        load_account("lucid-flex-50k", tmp_path)


def test_unquoted_flat_by_rejected(tmp_path, base):
    path = write(tmp_path, base)
    path.write_text(path.read_text().replace("'16:45'", "16:45"))  # YAML 1.1 reads 16:45 as 1005
    with pytest.raises(ValueError, match="flat_by"):
        load_account("lucid-flex-50k", tmp_path)


def test_mismatched_id_rejected(tmp_path, base):
    write(tmp_path, base, rel="lucid/pro-50k.yaml")
    with pytest.raises(ValueError, match="says lucid-flex-50k"):
        load_account("lucid-pro-50k", tmp_path)


def test_list_accounts_sorted(tmp_path, base):
    write(tmp_path, base, rel="lucid/pro-50k.yaml")
    write(tmp_path, base, rel="lucid/flex-50k.yaml")
    assert list_accounts(tmp_path) == ("lucid-flex-50k", "lucid-pro-50k")


def test_dump_round_trip(tmp_path, base):
    write(tmp_path, base)
    rules = load_account("lucid-flex-50k", tmp_path)
    (tmp_path / "lucid" / "flex-50k.yaml").write_text(dump_account(rules))
    assert load_account("lucid-flex-50k", tmp_path) == rules
