from minicord import MinicordClient
from minicord.gateway import Intents
from dotenv import load_dotenv
import os

load_dotenv()

def test_client_properties() -> None:
    client = MinicordClient(token=os.getenv("DISCORD_BOT_TOKEN", ""))

    assert client.base_url.endswith("/v10")
    assert client.timeout_seconds > 0

