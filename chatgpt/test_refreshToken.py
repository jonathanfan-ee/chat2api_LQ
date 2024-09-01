import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from httpx import AsyncClient
from chatgpt.refreshToken import chat_refresh

class MockResponse:
    def __init__(self, status_code, json):
        self.status_code = status_code
        self._json = json

    async def json(self):
        return self._json

@pytest.mark.asyncio
async def test_chat_refresh(monkeypatch):
    # Mock the necessary data for the test
    refresh_token = "mock_refresh_token"

    # Mock the HTTP response
    async def mock_post(url, data, headers, timeout):
        return MockResponse(status_code=200, json={"access_token": "mock_access_token"})

    # Patch the client.post method with the mock_post function
    monkeypatch.setattr(AsyncClient, "post", mock_post)

    # Call the chat_refresh function
    access_token = await chat_refresh(refresh_token)

    # Assert the result
    assert access_token is not None
    assert isinstance(access_token, str)
    assert len(access_token) > 0