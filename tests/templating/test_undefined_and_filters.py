"""RayspecUndefined strictness, filters, and the pure lint helpers."""

import pytest
from jinja2 import UndefinedError

from rayspec.errors import RayspecError
from rayspec.templating import (
    RayspecUndefined,
    TemplateCompileError,
    TemplateRenderError,
    fromjson,
    has_braces,
    has_gha_syntax,
    has_signal,
    regex_search,
)


def test_errors_derive_from_rayspec_error():
    assert issubclass(TemplateCompileError, RayspecError)
    assert issubclass(TemplateRenderError, RayspecError)
    err = TemplateCompileError(
        where="steps[2] (id: review).prompt", message="unexpected '}'", lineno=3
    )
    assert err.where == "steps[2] (id: review).prompt"
    assert err.lineno == 3
    assert "steps[2] (id: review).prompt" in str(err)
    assert "unexpected '}'" in str(err)
    assert "line 3" in str(err)


class TestRayspecUndefined:
    def test_chainable_on_access(self):
        u = RayspecUndefined(name="steps")
        assert isinstance(u.a.b["c"].d, RayspecUndefined)

    @pytest.mark.parametrize(
        "use",
        [
            str,
            lambda u: list(u),
            len,
            bool,
            lambda u: u == 1,
            lambda u: u != 1,
            hash,
            lambda u: "x" in u,
            lambda u: u + 1,
            lambda u: u(),
        ],
    )
    def test_strict_on_use(self, use):
        u = RayspecUndefined(name="inputs")
        with pytest.raises(UndefinedError, match="'inputs' is undefined"):
            use(u)

    def test_hint_is_part_of_the_message(self):
        u = RayspecUndefined(obj={"a": 1}, name="b", rayspec_hint="declare it under inputs:")
        with pytest.raises(UndefinedError) as exc:
            str(u)
        assert "has no attribute 'b'" in str(exc.value)
        assert "declare it under inputs:" in str(exc.value)
        # the hint survives chaining
        with pytest.raises(UndefinedError, match="declare it under inputs:"):
            str(u.c.d)

    def test_dunder_probe_raises_attribute_error(self):
        u = RayspecUndefined(name="x")
        assert not hasattr(u, "__deepcopy__")


class TestFilters:
    def test_fromjson(self):
        assert fromjson('{"a": [1, 2]}') == {"a": [1, 2]}
        with pytest.raises(ValueError, match="fromjson"):
            fromjson("{not json")
        with pytest.raises(ValueError, match="already"):
            fromjson({"a": 1})

    def test_regex_search(self):
        assert regex_search("score: 42", r"score: (\d+)", 1) == "42"
        assert regex_search("score: 42", r"score: (?P<n>\d+)", "n") == "42"
        assert regex_search("score: 42", r"\d+") == "42"
        assert isinstance(regex_search("nothing", r"\d+"), RayspecUndefined)
        with pytest.raises(ValueError, match="regex_search"):
            regex_search({"a": 1}, "a")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("DONE", True),
            ("some work\nDONE\n", True),
            ("some work\n\n  **DONE**  ", True),
            ("work\n_DONE_", True),
            ("work\n`DONE`", True),
            ("I am not DONE yet", False),
            ("done", False),
            ("DONE.", False),
            ("blah <signal>DONE</signal> blah", True),
            ("blah <signal> DONE </signal>", True),
            ("<signal>done</signal>", False),
            ("", False),
        ],
    )
    def test_has_signal(self, text, expected):
        assert has_signal(text, "DONE") is expected

    def test_has_signal_rejects_structured_output(self):
        with pytest.raises(ValueError, match=r"output\.field =="):
            has_signal({"status": "DONE"}, "DONE")


class TestLints:
    def test_has_braces(self):
        assert has_braces("{{ steps.a.ok }}")
        assert has_braces("{% if x %}y{% endif %}")
        assert not has_braces("steps.a.ok and inputs.x > 1")

    def test_has_gha_syntax(self):
        assert has_gha_syntax('echo "${{ inputs.x }}"')
        assert not has_gha_syntax('echo "${RAYSPEC_V1}" {{ inputs.x }}')
