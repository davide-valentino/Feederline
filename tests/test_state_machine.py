import json
from pathlib import Path


def test_direct_workflow_state_machine_has_closed_transitions_and_activity_actions():
    definition_path = Path(__file__).parents[1] / "infra" / "direct_same_region.asl.json"
    definition = json.loads(definition_path.read_text())
    states = definition["States"]

    assert definition["TimeoutSeconds"] == 1800
    assert definition["StartAt"] in states
    for state in states.values():
        targets = []
        if "Next" in state:
            targets.append(state["Next"])
        if "Default" in state:
            targets.append(state["Default"])
        targets.extend(choice["Next"] for choice in state.get("Choices", []))
        targets.extend(catcher["Next"] for catcher in state.get("Catch", []))
        assert all(target in states for target in targets)
        assert state["Type"] in ("Choice", "Pass", "Wait", "Task", "Fail", "Succeed")

    actions = {
        state["Parameters"]["Payload"]["action"]
        for state in states.values()
        if state["Type"] == "Task"
    }
    assert actions == {
        "resolve_link",
        "check_flow",
        "apply_link",
        "compensate_link",
        "record_failure",
        "resolve_unlink",
        "apply_unlink",
        "complete_link",
        "complete_unlink",
    }