import json

import hubble.keystore as keystore
import hubble.providers as providers


class MemoryKeyring:
    priority = 5

    def __init__(self):
        self.data = {}

    def set_password(self, service, name, secret):
        self.data[(service, name)] = secret

    def get_password(self, service, name):
        return self.data.get((service, name))

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


def use_file(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(providers, "HOME_DIR", tmp_path)


def test_keys_go_to_credential_store_not_the_file(tmp_path, monkeypatch):
    use_file(tmp_path, monkeypatch)
    kr = MemoryKeyring()
    monkeypatch.setattr(keystore, "_backend", lambda: kr)
    providers.save_provider(providers.ProviderConfig("groq", "https://api.groq.com/openai/v1", "sk-secret"))
    raw = (tmp_path / "providers.json").read_text(encoding="utf-8")
    assert "sk-secret" not in raw and "keyring:groq" in raw
    loaded = providers.load_providers({})
    assert loaded["groq"].api_key == "sk-secret"
    providers.remove_provider("groq")
    assert kr.data == {}


def test_falls_back_to_plain_text_without_a_store(tmp_path, monkeypatch):
    use_file(tmp_path, monkeypatch)
    monkeypatch.setattr(keystore, "_backend", lambda: None)
    providers.save_provider(providers.ProviderConfig("local", "http://localhost:11434/v1", "k1"))
    assert json.loads((tmp_path / "providers.json").read_text())["local"]["api_key"] == "k1"
    assert providers.load_providers({})["local"].api_key == "k1"


def test_secure_existing_keys_migrates_plain_text(tmp_path, monkeypatch):
    use_file(tmp_path, monkeypatch)
    (tmp_path / "providers.json").write_text(json.dumps({
        "a": {"base_url": "https://a/v1", "api_key": "plain-a"},
        "b": {"base_url": "https://b/v1", "api_key": "keyring:b"},
    }), encoding="utf-8")
    kr = MemoryKeyring()
    kr.set_password(keystore.SERVICE, "b", "stored-b")
    monkeypatch.setattr(keystore, "_backend", lambda: kr)
    assert providers.secure_existing_keys() == (1, 0)
    data = json.loads((tmp_path / "providers.json").read_text())
    assert data["a"]["api_key"] == "keyring:a"
    loaded = providers.load_providers({})
    assert loaded["a"].api_key == "plain-a" and loaded["b"].api_key == "stored-b"


def test_unreachable_store_skips_provider_instead_of_empty_key(tmp_path, monkeypatch):
    use_file(tmp_path, monkeypatch)
    (tmp_path / "providers.json").write_text(json.dumps({
        "a": {"base_url": "https://a/v1", "api_key": "keyring:a"}}), encoding="utf-8")
    monkeypatch.setattr(keystore, "_backend", lambda: None)
    assert "a" not in providers.load_providers({})
