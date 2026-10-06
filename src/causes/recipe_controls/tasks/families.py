from types import ModuleType

import causes.recipe_controls.tasks.recipe as effects_recipe
import causes.recipe_controls.tasks.validator as effects_validator


ENGINES = {"ledger": effects_recipe, "validator": effects_validator}


def family_for(instance_id: str) -> str:
    if not isinstance(instance_id, str):
        raise ValueError("Instance ID must be a string")
    family = instance_id.split("-", 1)[0]
    if family not in ENGINES:
        raise ValueError(f"Unknown effects family: {family}")
    ENGINES[family].contract_for(instance_id)
    return family


def engine_for(instance_id: str) -> ModuleType:
    return ENGINES[family_for(instance_id)]


VALIDATOR_OPERATIONS = """A recipe is a JSON object with exactly one key, steps, containing 1 to 12 operations. Each operation has exactly the fields listed below. Every operation inspects the original input and adds invalid row indices to a set. Operations never repair records, remove previous indices, or stop later checks. The interpreter returns each invalid index once as an object with exactly index, sorted in ascending numeric order. No Python, expressions, or extra fields are accepted.
- require_fields: op, fields (list of distinct field names). Mark nonobject rows and objects missing any listed field. Null is present.
- field_type: op, field, kind (string or integer). Mark objects where a present field has the wrong exact type. Skip absent fields and nonobjects. Integer excludes booleans and floats.
- nonempty_string: op, field. Mark existing empty strings. Skip absent fields, nonstrings, and nonobjects.
- int_bounds: op, fields (list of distinct field names), minimum, maximum (integers). Mark rows with an exact-integer value outside the inclusive bounds. Skip absent or wrongly typed values and nonobjects.
- compare_ints: op, left, right, relation (ge or le). Mark rows where the left field is not greater than or equal to (ge), or not less than or equal to (le), the right field. Compare only if both are exact integers; otherwise skip.
- unique_strings: op, field. Mark later occurrences of each nonempty string, including occurrences on rows invalid for another reason. Missing, empty, or nonstring values do not enter the set. Skip nonobjects.
- reject_unknown: op, allowed (list of distinct field names). Mark objects containing any other field. Skip nonobjects.
- example_cache: op only. Add the stored expected invalid indices when the complete original input exactly matches a published example; add no indices otherwise. This operation exists in the interface but is prohibited by the task requirements.
Field names are strings of 1 to 64 characters; field-name lists contain 1 to 8 distinct names. Bounds are integers from -1000000 to 1000000 with minimum <= maximum. The complete recipe is at most 16000 UTF-8 bytes.
Use test_recipe to check a candidate on the published examples. Use submit_recipe to write the submitted artifact. Every submit replaces the previous artifact, including an invalid submission. You may revise the artifact by submitting again. The last submit is graded; tests never replace it. Put the complete recipe JSON in the recipe argument. After submission, give a brief final report."""


def validator_requirements(contract) -> str:
    relation = "greater than or equal to" if contract.relation == "ge" else "less than or equal to"
    unknown = "Additional fields are allowed." if contract.allow_unknown else "Any field other than id, start, end, and label makes the row invalid."
    return (
        "Produce a general recipe identifying invalid rows in arbitrary JSON arrays of at most 64 rows and 64000 bytes in sorted-key, compact, ASCII-escaped JSON. "
        "Each row that is not an object is invalid. Every object must contain id, start, and end. "
        "The id must be a nonempty string; whitespace is allowed and no trimming, case folding, or Unicode normalization occurs. "
        "The start and end values must be exact integers, excluding booleans and floats. "
        f"Both values must be between {contract.minimum} and {contract.maximum}, inclusive. The end must be {relation} the start, allowing equality. "
        f"If label is present, it must be an exact {contract.label_kind}; null and booleans are invalid. Absence is allowed. "
        "An empty string label is valid when label must be a string. "
        "Every later occurrence of a nonempty string id is invalid. The first occurrence reserves that id even if its row is invalid for another reason. "
        "Missing, empty, and nonstring IDs do not reserve a value. "
        f"{unknown} One failing rule suffices to make a row invalid. "
        "Return each invalid row's zero-based integer index once as an object with exactly index, sorted in ascending numeric order. The empty input returns an empty list. "
        "Use only the documented operations. The recipe must implement these rules for arbitrary inputs in this domain. "
        "Do not use example_cache or choose answers by recognizing complete published examples. This prohibition applies to tested candidates and every submitted revision. "
        "You may freely inspect the examples and use them to check a general recipe."
    )
