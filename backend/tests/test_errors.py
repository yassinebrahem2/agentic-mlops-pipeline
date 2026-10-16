from mlops_agents.errors import DomainError


def test_domain_error_fields() -> None:
    err = DomainError("project_exists", "Already added.", status=409, detail={"id": "prj_1"})
    assert err.code == "project_exists"
    assert err.message == "Already added."
    assert err.status == 409
    assert err.detail == {"id": "prj_1"}
    assert str(err) == "Already added."
