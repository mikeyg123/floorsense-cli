"""Exit codes are the CLI's contract with any script wrapping it, so they are
pinned here by number. Changing one of these is a breaking change."""

import pytest

from fs_cli import errors as e


@pytest.mark.parametrize("exc,code", [
    (e.UsageError("x"), 2),
    (e.LoginRequired("x"), 3),
    (e.AuthFailed("x"), 4),
    (e.CommError("x"), 5),
    (e.NoDeskAvailable("x"), 6),
    (e.UserRejected("x"), 7),
    (e.NotFound("x"), 8),
    (e.BusinessRuleRefused("x"), 9),
])
def test_exit_codes(exc, code):
    assert e.exit_code_for(exc) == code


def test_an_unexpected_exception_is_never_mistaken_for_a_clean_failure():
    assert e.exit_code_for(ValueError("boom")) == e.ExitCode.UNEXPECTED == 1


def test_hints_are_optional_and_carried():
    assert e.NotFound("no desk").hint is None
    assert e.NotFound("no desk", hint="Try the full key.").hint == \
        "Try the full key."


def test_every_error_is_an_fs_error():
    for cls in (e.UsageError, e.LoginRequired, e.AuthFailed, e.CommError,
                e.NoDeskAvailable, e.UserRejected, e.NotFound,
                e.BusinessRuleRefused):
        assert issubclass(cls, e.FsError)


def test_exit_codes_are_distinct():
    codes = [v for k, v in vars(e.ExitCode).items() if not k.startswith("_")]
    assert len(codes) == len(set(codes))
