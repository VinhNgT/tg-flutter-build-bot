"""Jenkins REST API client for triggering and querying builds."""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class JenkinsClient:
    """Triggers parameterized Jenkins builds and queries build history."""

    def __init__(
        self, url: str, user: str, api_token: str, job_name: str
    ) -> None:
        self.base_url = url.rstrip("/")
        self.job_name = job_name
        self._auth = aiohttp.BasicAuth(user, api_token)

    @property
    def job_url(self) -> str:
        return f"{self.base_url}/job/{self.job_name}"

    async def trigger_build(
        self, branch: str, callback_url: str
    ) -> int | None:
        """Trigger a parameterized Jenkins build.

        Returns the queue item ID, or None on failure.
        """
        url = f"{self.job_url}/buildWithParameters"
        params = {
            "BRANCH": branch,
            "BOT_CALLBACK_URL": callback_url,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, params=params, auth=self._auth
            ) as resp:
                if resp.status == 201:
                    queue_url = resp.headers.get("Location", "")
                    try:
                        queue_id = int(
                            queue_url.rstrip("/").split("/")[-1]
                        )
                        logger.info("Build queued: queue_id=%d", queue_id)
                        return queue_id
                    except (ValueError, IndexError):
                        logger.error(
                            "Could not parse queue ID from: %s", queue_url
                        )
                        return None
                else:
                    body = await resp.text()
                    logger.error(
                        "Jenkins trigger failed: %d — %s",
                        resp.status,
                        body[:200],
                    )
                    return None

    async def get_build_status(self, build_number: int) -> dict | None:
        """Query a specific build's status."""
        url = f"{self.job_url}/{build_number}/api/json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=self._auth) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None

    async def get_recent_builds(self, count: int = 5) -> list[dict]:
        """Get recent build history for /recent command."""
        url = (
            f"{self.job_url}/api/json"
            f"?tree=builds[number,result,timestamp,duration]"
            f"{{0,{count}}}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=self._auth) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("builds", [])
                return []

    async def get_queue_item(self, queue_id: int) -> dict | None:
        """Get info about a queued build item."""
        url = f"{self.base_url}/queue/item/{queue_id}/api/json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=self._auth) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
