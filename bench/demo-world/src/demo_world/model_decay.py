"""The `model_decay` rule: the outcomes of one group of bookings turn against what the first
model learned, for the bookings arriving in a window.

In the source, long lead times cancel more, and the first model learned it. A booking that
follows the rule cancels when its lead time is below a cut, and does not otherwise. The share
of the group's bookings that follow it ramps with their arrival date over the window. Only the
labels change: the bookings, which the company scores and checks for drift, stay as they are.
"""

import hashlib
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


class DecayError(Exception):
    """The source cannot give the rule what it needs."""


def _draw(purpose: str, booking_id: int) -> int:
    # A digest, not `hash()` (salted per process) or a random state: a rerun or a resumed run
    # picks the same bookings and dates.
    digest = hashlib.sha256(f"model_decay|{purpose}|{booking_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class Rule:
    """The rule for the bookings whose every `group` column holds its value, arriving from
    `start` to `end` (both included); `max_share` of them follow it at the end."""

    group: tuple[tuple[str, str], ...]
    start: date
    end: date
    max_share: float

    def share(self, arrival: date) -> float:
        """The share of the group's bookings arriving on `arrival` that follow the rule."""
        if not self.start <= arrival <= self.end:
            return 0.0
        return self.max_share * (arrival - self.start).days / (self.end - self.start).days

    def _in_group(self, loaded: pd.DataFrame) -> pd.Series:
        matches = pd.Series(True, index=loaded.index)
        for column, value in self.group:
            matches &= loaded[column].eq(value).fillna(False).astype(bool)
        return matches

    def _scope(self, loaded: pd.DataFrame, origin: date) -> pd.Series:
        # Only bookings made on or after the world's first day: an earlier booking and perhaps
        # its label are history the backfill already wrote, so a changed label could be
        # released twice, or on a day already played.
        arrival = loaded["arrival_date"]
        return (
            self._in_group(loaded)
            & (arrival >= pd.Timestamp(self.start))
            & (arrival <= pd.Timestamp(self.end))
            & (loaded["booking_date"] >= pd.Timestamp(origin))
        )

    def followers(self, loaded: pd.DataFrame, origin: date) -> pd.Series:
        """Which bookings follow the rule, in a world first played on `origin`."""
        return self._followers(loaded, self._scope(loaded, origin))

    def _followers(self, loaded: pd.DataFrame, scope: pd.Series) -> pd.Series:
        rows = loaded.loc[scope, ["booking_id", "arrival_date"]]
        draws = [
            _draw("follows", booking_id) / 2**64 < self.share(arrival.date())
            for booking_id, arrival in zip(rows["booking_id"], rows["arrival_date"], strict=True)
        ]
        return pd.Series(draws, index=rows.index, dtype=bool).reindex(
            loaded.index, fill_value=False
        )

    def _pre_window_rate(self, loaded: pd.DataFrame) -> float:
        before = self._in_group(loaded) & (loaded["arrival_date"] < pd.Timestamp(self.start))
        if not before.any():
            raise DecayError(
                f"no booking of the group {dict(self.group)} arrived before {self.start}, "
                "so it has no cancellation rate to keep"
            )
        return float(loaded.loc[before, "is_canceled"].mean())

    def cut(self, loaded: pd.DataFrame, origin: date) -> int:
        """The lead time below which a follower cancels.

        It keeps the group's cancellation rate, over the bookings the rule may change, at the
        group's rate before the window: the cut that brings that rate (followers by the rule,
        the others as in the source) closest to it, the smallest one on a tie.
        """
        scope = self._scope(loaded, origin)
        return self._cut(loaded, scope, self._followers(loaded, scope))

    def _cut(self, loaded: pd.DataFrame, scope: pd.Series, follows: pd.Series) -> int:
        others = int(loaded.loc[scope & ~follows, "is_canceled"].sum())
        wanted = self._pre_window_rate(loaded) * int(scope.sum()) - others
        leads = np.sort(loaded.loc[follows, "lead_time"].to_numpy())
        if leads.size == 0:
            return 0
        candidates = np.unique(np.append(leads, leads[-1] + 1))
        below = np.searchsorted(leads, candidates, side="left")
        return int(candidates[np.argmin(np.abs(below - wanted))])

    def relabel(self, loaded: pd.DataFrame, origin: date) -> pd.DataFrame:
        """The source with the followers' outcomes set by the rule.

        A booking turned into a cancellation is cancelled on a day drawn between its booking
        and its arrival, and one no longer cancelled is labelled on its arrival, so the
        overview's release rule releases each changed label once, never before its booking.
        """
        scope = self._scope(loaded, origin)
        follows = self._followers(loaded, scope)
        outcome = (loaded["lead_time"] < self._cut(loaded, scope, follows)).astype("int64")
        flipped = follows & (outcome != loaded["is_canceled"])
        cancelled = flipped & (outcome == 1)
        relabelled = loaded.copy()
        relabelled.loc[flipped, "is_canceled"] = outcome[flipped]
        relabelled.loc[flipped & ~cancelled, "label_date"] = loaded.loc[
            flipped & ~cancelled, "arrival_date"
        ]
        new = loaded.loc[cancelled, ["booking_id", "lead_time", "booking_date"]]
        # Before the arrival day, as a cancellation is; a booking made for the same day is
        # cancelled that day.
        days = [
            _draw("cancelled", booking_id) % lead if lead > 0 else 0
            for booking_id, lead in zip(new["booking_id"], new["lead_time"], strict=True)
        ]
        relabelled.loc[cancelled, "label_date"] = new["booking_date"] + pd.to_timedelta(
            days, unit="D"
        )
        return relabelled
