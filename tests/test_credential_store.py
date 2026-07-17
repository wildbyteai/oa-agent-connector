import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from oa_agent_connector.credential_store import CredentialStoreError, SystemCredentialStore


class FakeBackend:
    priority = 1

    def __init__(self):
        self.values = {}
        self.calls = []

    def set_password(self, service, username, password):
        self.calls.append(("set", service, username, password))
        self.values[(service, username)] = password

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        return self.values.get((service, username))

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        self.values.pop((service, username), None)


class SystemCredentialStoreTest(unittest.TestCase):
    def test_round_trip_is_normalized_and_scoped_to_session(self):
        backend = FakeBackend()
        store = SystemCredentialStore(backend=backend)

        store.save("https://EXAMPLE.invalid/oa/", "work", "u001", "secret-password")

        self.assertEqual(
            store.load("https://example.invalid/oa", "work", "u001"),
            "secret-password",
        )
        self.assertIsNone(store.load("https://example.invalid/oa", "other", "u001"))
        service = backend.calls[0][1]
        self.assertTrue(service.startswith("oa-agent-connector:"))
        self.assertNotIn("secret-password", service)
        self.assertNotIn("example.invalid", service)

    def test_new_login_replaces_previous_account_for_the_same_session(self):
        backend = FakeBackend()
        store = SystemCredentialStore(backend=backend)

        store.save("https://example.invalid/oa/", "work", "old-user", "old-secret")
        store.save("https://example.invalid/oa/", "work", "new-user", "new-secret")

        self.assertIsNone(store.load("https://example.invalid/oa/", "work", "old-user"))
        self.assertEqual(store.load("https://example.invalid/oa/", "work", "new-user"), "new-secret")
        self.assertEqual(len(backend.values), 1)

    def test_same_session_is_isolated_between_connector_state_directories(self):
        backend = FakeBackend()
        first = SystemCredentialStore(backend=backend, namespace="/users/a/.oa-agent-connector")
        second = SystemCredentialStore(backend=backend, namespace="/users/a/other-oa-state")

        first.save("https://example.invalid/oa/", "default", "u001", "first-secret")

        self.assertEqual(
            first.load("https://example.invalid/oa/", "default", "u001"),
            "first-secret",
        )
        self.assertIsNone(second.load("https://example.invalid/oa/", "default", "u001"))

    def test_unavailable_backend_fails_closed(self):
        class UnavailableBackend:
            priority = 0

        store = SystemCredentialStore(backend=UnavailableBackend())

        with self.assertRaises(CredentialStoreError):
            store.save("https://example.invalid/oa/", "work", "u001", "secret-password")

    def test_plaintext_or_unknown_keyring_backend_is_rejected(self):
        class PlaintextBackend(FakeBackend):
            pass

        PlaintextBackend.__module__ = "keyrings.alt.file"
        fake_keyring = SimpleNamespace(get_keyring=lambda: PlaintextBackend())

        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            with self.assertRaises(CredentialStoreError):
                SystemCredentialStore().save(
                    "https://example.invalid/oa/",
                    "work",
                    "u001",
                    "secret-password",
                )

    def test_windows_native_keyring_backend_is_allowed(self):
        class WindowsBackend(FakeBackend):
            pass

        WindowsBackend.__module__ = "keyring.backends.Windows"
        backend = WindowsBackend()
        fake_keyring = SimpleNamespace(get_keyring=lambda: backend)

        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            store = SystemCredentialStore(namespace="windows-state")
            store.save("https://example.invalid/oa/", "default", "u001", "secret-password")
            self.assertEqual(
                store.load("https://example.invalid/oa/", "default", "u001"),
                "secret-password",
            )


if __name__ == "__main__":
    unittest.main()
