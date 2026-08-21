from rayspec.store.model import StepRecord


def test_step_record_has_approved():
    rec = StepRecord(path="g", id="g", kind="approve", approved=True)
    assert rec.approved is True and StepRecord(path="a", id="a", kind="shell").approved is None
