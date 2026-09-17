from pydantic import BaseModel

from autoregent.heal_executor import apply_field_mapping, validate_healed_payload


class AccountBalance(BaseModel):
    account_id: str
    balance: float


def test_apply_field_mapping_success():
    mapping = {"account_id": "id", "balance": "current_balance"}
    original = {"id": "acc_1", "current_balance": 42.5}
    healed = apply_field_mapping(mapping, original)
    assert healed == {"account_id": "acc_1", "balance": 42.5}


def test_apply_field_mapping_missing_source_fails():
    mapping = {"account_id": "id", "balance": "does_not_exist"}
    original = {"id": "acc_1", "current_balance": 42.5}
    assert apply_field_mapping(mapping, original) is None


def test_apply_field_mapping_never_invents_values():
    # An empty mapping heals to an empty dict, never a synthesized default.
    assert apply_field_mapping({}, {"id": "acc_1"}) == {}


def test_validate_healed_payload_accepts_matching_shape():
    healed = {"account_id": "acc_1", "balance": 42.5}
    assert validate_healed_payload(AccountBalance, healed) is None


def test_validate_healed_payload_rejects_missing_field():
    healed = {"account_id": "acc_1"}
    assert validate_healed_payload(AccountBalance, healed) is not None


def test_validate_healed_payload_strict_mode_rejects_type_drift():
    # Strict mode must NOT silently coerce a stringified number -- that's
    # exactly the drift class the validation gate exists to catch.
    healed = {"account_id": "acc_1", "balance": "42.5"}
    assert validate_healed_payload(AccountBalance, healed) is not None
