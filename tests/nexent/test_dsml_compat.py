"""Tests for the optional DeepSeek DSML compatibility layer."""

from __future__ import annotations

from deploy.nexent_dsml_compat.nexent_dsml_compat import normalize_dsml_tool_calls


def test_normalizes_fullwidth_dsml_tool_call() -> None:
    output = """<｜DSML｜ calls>
<｜DSML｜ invoke name="read_skill_md">
<｜DSML｜ parameter name="skill_name" string="true">ontology-change-draft</｜DSML｜ parameter>
<｜DSML｜ parameter name="additional_files" string="false">[]</｜DSML｜ parameter>
</｜DSML｜ invoke>
</｜DSML｜ calls>"""

    assert normalize_dsml_tool_calls(output) == (
        "<code>\n"
        "read_skill_md(skill_name='ontology-change-draft', additional_files=[])\n"
        "</code>"
    )


def test_normalizes_ascii_dsml_and_multiple_invocations() -> None:
    output = """<|DSML| calls>
<|DSML| invoke name="get_ontology_governance_status"></|DSML| invoke>
<|DSML| invoke name="list_ontology_changes">
<|DSML| parameter name="limit" string="false">50</|DSML| parameter>
</|DSML| invoke>
</|DSML| calls>"""

    assert normalize_dsml_tool_calls(output) == (
        "<code>\n"
        "get_ontology_governance_status()\n"
        "list_ontology_changes(limit=50)\n"
        "</code>"
    )


def test_normalizes_dsml_with_whitespace_between_control_characters() -> None:
    output = """< | DSML | calls >
< | DSML | invoke name="get_ontology">
</ | DSML | invoke >
</ | DSML | calls >"""

    assert normalize_dsml_tool_calls(output) == ("<code>\nget_ontology()\n</code>")


def test_string_parameter_is_preserved_without_reinterpreting_json() -> None:
    output = """<|DSML| calls>
<|DSML| invoke name="review_ontology_change">
<|DSML| parameter name="payload" string="true">{"confirm": true}</|DSML| parameter>
</|DSML| invoke>
</|DSML| calls>"""

    assert normalize_dsml_tool_calls(output) == (
        "<code>\nreview_ontology_change(payload='{\"confirm\": true}')\n</code>"
    )


def test_unknown_or_unsafe_tool_name_is_left_for_protocol_validation() -> None:
    output = """<|DSML| calls>
<|DSML| invoke name="os.system">
</|DSML| invoke>
</|DSML| calls>"""

    assert normalize_dsml_tool_calls(output) == output


def test_plain_text_and_existing_code_actions_are_unchanged() -> None:
    assert normalize_dsml_tool_calls("普通回答") == "普通回答"
    assert (
        normalize_dsml_tool_calls("<code>get_ontology()</code>")
        == "<code>get_ontology()</code>"
    )
