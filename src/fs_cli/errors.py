"""Exceptions and their exit codes.

One class per exit code, so a command never has to know a number: it raises
the exception that describes what went wrong and `cli.py` maps it. The codes
themselves are the CLI's contract with any script wrapping it, so they are
defined once, here, and tested.
"""

__all__ = [
    "ExitCode", "FsError", "UsageError", "LoginRequired", "AuthFailed",
    "InvalidCredentials", "CommError", "NoDeskAvailable", "UserRejected",
    "NotFound", "BusinessRuleRefused", "exit_code_for",
]


class ExitCode:
    OK = 0
    UNEXPECTED = 1
    USAGE = 2
    LOGIN_REQUIRED = 3
    AUTH_FAILED = 4
    COMM = 5
    NO_DESK = 6
    USER_REJECTED = 7
    NOT_FOUND = 8
    BUSINESS_RULE = 9


class FsError(Exception):
    """Base for everything the tool raises deliberately. Anything else
    escaping to `cli.py` is a bug and exits UNEXPECTED."""
    code = ExitCode.UNEXPECTED
    #: An optional second line telling the user what to do about it.
    hint = None

    def __init__(self, message, hint=None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class UsageError(FsError):
    code = ExitCode.USAGE


class LoginRequired(FsError):
    """Dead session with --no-login, MFA declined, or MFA timed out. Not an
    error condition in the ordinary sense -- the manual is explicit that
    "session probably dead" is a normal state (§10)."""
    code = ExitCode.LOGIN_REQUIRED


class AuthFailed(FsError):
    """Okta rejected the login. Distinct from LoginRequired: one needs a
    tap, the other a correct credential -- or, for `LOCKED_OUT`, an
    unlock the account owner has to sort out with Okta."""
    code = ExitCode.AUTH_FAILED


class InvalidCredentials(AuthFailed):
    """`AuthFailed` specifically because Okta rejected the password or
    username itself (401 / `E0000004`), not `LOCKED_OUT`. Distinguished
    so `session.py` can forget a stored password on this and NOT on a
    locked-out account, where the password was never the problem."""


class CommError(FsError):
    """Network failure, 5xx, or the HTML CSRF page where JSON was due (§7)."""
    code = ExitCode.COMM


class NoDeskAvailable(FsError):
    code = ExitCode.NO_DESK


class UserRejected(FsError):
    code = ExitCode.USER_REJECTED


class NotFound(FsError):
    """A desk, person, group or team that doesn't resolve."""
    code = ExitCode.NOT_FOUND


class BusinessRuleRefused(FsError):
    """The server said no for a policy reason: desk limit, `code: 64`,
    outside the booking window (§8)."""
    code = ExitCode.BUSINESS_RULE


def exit_code_for(exc):
    """Map any exception to an exit code. Unknown exceptions are UNEXPECTED
    rather than being allowed to masquerade as a clean failure."""
    return getattr(exc, "code", ExitCode.UNEXPECTED) \
        if isinstance(exc, FsError) else ExitCode.UNEXPECTED
