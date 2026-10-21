"""Phone notifications (ntfy) and the dead-man's-switch heartbeat (PLAN.md §6).

Sending never raises: a notification problem must not stop trading or exits.
Repeated warnings with the same key are sent at most once per cooldown.
"""
import datetime as dt
from dataclasses import dataclass, field

import httpx
from loguru import logger

COOLDOWN = dt.timedelta(hours=1)
HEARTBEAT_EVERY = dt.timedelta(minutes=5)

@dataclass
class Notifier:
  """Send to an ntfy topic; without a topic, only log."""

  topic: str | None = None
  server: str = "https://ntfy.sh"
  heartbeat_url: str | None = None
  sent: list[tuple[str, str, str]] = field(default_factory=list)
  _last_key: dict[str, dt.datetime] = field(default_factory=dict)
  _last_beat: dt.datetime | None = None

  async def send(
    self, title: str, message: str, *, priority: str = "default",
    tags: str = "", key: str | None = None,
  ) -> None:
    """Notify. `key` + cooldown stops the same warning repeating every pass."""
    now = dt.datetime.now(dt.UTC)
    if key is not None:
      last = self._last_key.get(key)
      if last is not None and now - last < COOLDOWN:
        return
      self._last_key[key] = now
    self.sent.append((title, message, priority))
    logger.info("Notify [{}] {}: {}", priority, title, message)
    if not self.topic:
      return
    headers = {"Title": title.encode("ascii", "replace").decode(),
               "Priority": priority}
    if tags:
      headers["Tags"] = tags
    try:
      async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{self.server.rstrip('/')}/{self.topic}",
                              content=message.encode(), headers=headers)
        r.raise_for_status()
    except httpx.HTTPError as e:
      logger.warning("ntfy failed: {}", e)

  async def heartbeat(self) -> None:
    """Ping the dead-man's switch (at most every 5 minutes)."""
    if not self.heartbeat_url:
      return
    now = dt.datetime.now(dt.UTC)
    if self._last_beat is not None and now - self._last_beat < HEARTBEAT_EVERY:
      return
    self._last_beat = now
    try:
      async with httpx.AsyncClient(timeout=10) as client:
        await client.get(self.heartbeat_url)
    except httpx.HTTPError as e:
      logger.warning("Heartbeat failed: {}", e)
