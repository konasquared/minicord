from minicord import MinicordClient


def test_client_properties() -> None:
    client = MinicordClient(token="test-token")

    assert client.base_url.endswith("/v10")
    assert client.timeout_seconds > 0
