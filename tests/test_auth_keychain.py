"""Keychain account scoping (`auth._account_key` and the four functions
built on it).

Regression coverage for a real bug: the keychain was keyed on `okta_user`
alone, with no origin/org component, even though `--url` makes switching
between two Floorsense deployments a first-class flow. Two deployments
that happen to share an Okta local part would silently read and overwrite
each other's password. No network, no real keyring -- a dict stands in for
the OS keychain.
"""

import keyring.errors
import pytest

from fs_cli import auth


class FakeKeyring:
    """In-memory stand-in for the `keyring` module's three entry points,
    keyed exactly like the real backend: (service, account) -> password."""

    #: `forget_password` catches this specific exception type at
    #: `keyring.errors.PasswordDeleteError` -- exposed the same way on the
    #: fake so `auth.keyring = fake` doesn't break that except clause.
    errors = keyring.errors

    def __init__(self):
        self.store = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise keyring.errors.PasswordDeleteError()
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(auth, "keyring", fake)
    return fake


# --- _account_key ------------------------------------------------------

def test_account_key_is_bare_username_at_the_default_origin():
    assert auth._account_key("jamie.baker") == "jamie.baker"
    assert auth._account_key(
        "jamie.baker", origin=auth.FLOORSENSE_ORIGIN) == "jamie.baker"


def test_account_key_is_scoped_by_host_at_a_non_default_origin():
    assert auth._account_key("jamie.baker", origin="https://other.example") \
        == "other.example:jamie.baker"


# --- store/get round-trip, scoped by origin -----------------------------

def test_password_stored_at_one_origin_is_not_found_at_another(fake_keyring):
    auth.store_password("jamie.baker", "corp-pw", origin=auth.FLOORSENSE_ORIGIN)
    auth.store_password("jamie.baker", "other-pw",
                        origin="https://other.example")

    assert auth.get_password("jamie.baker", prompt=lambda _: "unused",
                             origin=auth.FLOORSENSE_ORIGIN) == "corp-pw"
    assert auth.get_password("jamie.baker", prompt=lambda _: "unused",
                             origin="https://other.example") == "other-pw"


def test_get_password_prompts_when_nothing_stored_at_this_origin(
        fake_keyring):
    auth.store_password("jamie.baker", "corp-pw", origin=auth.FLOORSENSE_ORIGIN)

    prompted = auth.get_password(
        "jamie.baker", prompt=lambda _: "typed-fresh",
        origin="https://other.example")
    assert prompted == "typed-fresh"


def test_has_stored_password_is_scoped_by_origin(fake_keyring):
    auth.store_password("jamie.baker", "corp-pw", origin=auth.FLOORSENSE_ORIGIN)

    assert auth.has_stored_password("jamie.baker",
                                    origin=auth.FLOORSENSE_ORIGIN)
    assert not auth.has_stored_password("jamie.baker",
                                        origin="https://other.example")


def test_forget_password_only_clears_the_matching_origin(fake_keyring):
    auth.store_password("jamie.baker", "corp-pw", origin=auth.FLOORSENSE_ORIGIN)
    auth.store_password("jamie.baker", "other-pw",
                        origin="https://other.example")

    auth.forget_password("jamie.baker", origin="https://other.example")

    assert auth.has_stored_password("jamie.baker",
                                    origin=auth.FLOORSENSE_ORIGIN)
    assert not auth.has_stored_password("jamie.baker",
                                        origin="https://other.example")
