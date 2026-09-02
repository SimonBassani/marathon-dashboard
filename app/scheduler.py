"""
Hintergrund-Sync.

Ein asyncio-Task statt eines zweiten Containers oder eines Cron-Daemons. Der
Garmin-Sync ist blockierendes I/O, läuft deshalb in einem Thread-Pool und
blockiert die Weboberfläche nicht.

Rhythmus:
  * beim Start ein kurzer Sync (3 Tage), damit sofort etwas da ist
  * tagsüber alle SYNC_INTERVAL_MIN Minuten die letzten 3 Tage
  * einmal täglich früh ein voller Rückblick über SYNC_BACKFILL_DAYS Tage
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from . import garmin, oauth, plan

log = logging.getLogger("scheduler")

INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "45"))
BACKFILL_DAYS = int(os.environ.get("SYNC_BACKFILL_DAYS", "45"))
DAY_START = int(os.environ.get("SYNC_START_HOUR", "6"))
DAY_END = int(os.environ.get("SYNC_END_HOUR", "23"))


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_backfill: date | None = None

    async def start(self) -> None:
        if not os.environ.get("GARMIN_EMAIL"):
            log.warning("GARMIN_EMAIL fehlt — der Sync bleibt aus, das Dashboard läuft trotzdem")
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_sync_now(self, days: int = 3) -> dict:
        """Vom Aktualisieren-Knopf im Dashboard. Der Lock verhindert, dass zwei
        Syncs gleichzeitig auf dieselbe Datei schreiben."""
        if self._lock.locked():
            return {"ok": False, "error": "Ein Sync läuft bereits."}
        async with self._lock:
            return await asyncio.to_thread(garmin.sync, days, 4, "manual")

    async def _sync(self, days: int, mode: str) -> None:
        async with self._lock:
            result = await asyncio.to_thread(garmin.sync, days, 4, mode)
        log.info("Sync (%s): %s", mode, result)
        # Nach jedem Sync den Plan mit der Realität abgleichen.
        await asyncio.to_thread(_match_plan)

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        try:
            await self._sync(3, "startup")
        except Exception as exc:  # noqa: BLE001
            log.error("Start-Sync fehlgeschlagen: %s", exc)

        while True:
            try:
                now = datetime.now()
                today = now.date()

                if self._last_backfill != today and now.hour == DAY_START:
                    await self._sync(BACKFILL_DAYS, "backfill")
                    self._last_backfill = today
                elif DAY_START <= now.hour < DAY_END:
                    await self._sync(3, "auto")

                await asyncio.to_thread(oauth.cleanup_expired)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — der Loop darf nie sterben
                log.error("Sync-Schleife: %s", exc)

            await asyncio.sleep(INTERVAL_MIN * 60)


def _match_plan() -> None:
    today = date.today()
    try:
        plan.match_completed(today - timedelta(days=21), today)
    except Exception as exc:  # noqa: BLE001
        log.warning("Plan-Abgleich: %s", exc)
