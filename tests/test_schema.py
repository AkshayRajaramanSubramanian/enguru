import json
from pathlib import Path

import pytest

from enguru_lib.schema import load_schema, validate

FIX = Path(__file__).parent / "fixtures" / "schemas"
NAMES = ["extracted", "chunk", "digest", "topic-map", "bank", "verdict", "record"]


def _extracted(**ev):
    e = {"label": "choice", "paraphrase": "x", "files": [], "confidence": 0.5, "source_turn_ids": ["t1"]}
    e.update(ev)
    return {"chunk_id": "k", "events": [e]}


def test_type_required_enum_errors_have_paths():
    s = load_schema("extracted")
    errs = validate(_extracted(label="maybe"), s)
    assert any("$.events[0].label" in e for e in errs)
    errs = validate({"events": []}, s)
    assert any("chunk_id" in e and e.startswith("$") for e in errs)
    errs = validate({"chunk_id": 3, "events": []}, s)
    assert any(e.startswith("$.chunk_id") for e in errs)


def test_additional_properties_false():
    errs = validate(_extracted(bogus=1), load_schema("extracted"))
    assert any("$.events[0]" in e and "bogus" in e for e in errs)


def test_bounds():
    s = load_schema("extracted")
    assert validate(_extracted(paraphrase="x" * 200), s) == []
    assert validate(_extracted(paraphrase="x" * 201), s)
    assert validate(_extracted(confidence=1.5), s)
    assert validate(_extracted(source_turn_ids=[]), s)
    bank = json.loads((FIX / "bank.json").read_text())
    bank["findings"][0]["ladder"] = bank["findings"][0]["ladder"][:2]
    assert any("ladder" in e for e in validate(bank, load_schema("bank")))


def test_bool_is_not_integer_or_number():
    assert validate(True, {"type": "integer"})
    assert validate(True, {"type": "number"})
    assert validate(1, {"type": "integer"}) == []
    assert validate(1.5, {"type": "number"}) == []
    assert validate(None, {"type": ["string", "null"]}) == []
    assert validate(3, {"type": ["string", "null"]})


@pytest.mark.parametrize("name", NAMES)
def test_all_schemas_load_and_accept_minimal_examples(name):
    inst = json.loads((FIX / ("%s.json" % name)).read_text())
    assert validate(inst, load_schema(name)) == []


def test_topic_map_module_bounds():
    inst = json.loads((FIX / "topic-map.json").read_text())
    inst["modules"] = inst["modules"][:3]
    assert validate(inst, load_schema("topic-map"))
