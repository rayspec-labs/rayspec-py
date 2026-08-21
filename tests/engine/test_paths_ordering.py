from rayspec.engine.paths import StepPath


def test_sorting_mixed_indexed_and_unindexed_paths():  # S4
    paths = [StepPath.parse(p) for p in ["build[1]", "build", "a/b[1]", "a/b", "a"]]
    assert [str(p) for p in sorted(paths)] == ["a", "a/b", "a/b[1]", "build", "build[1]"]
    assert StepPath.parse("a") < StepPath.parse("b")
    assert not (StepPath.parse("a") < StepPath.parse("a"))


def test_parse_normalises_leading_zero_index():
    assert str(StepPath.parse("build[007]")) == "build[7]"
