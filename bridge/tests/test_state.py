from state import DedupStore


def test_claim_survives_new_store_instance(tmp_path):
    path = tmp_path / "bridge.sqlite3"

    assert DedupStore(path).claim("account:1:message:42")
    assert not DedupStore(path).claim("account:1:message:42")
    assert DedupStore(path).claim("account:1:message:43")
