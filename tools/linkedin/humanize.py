"""Human-like pacing for the LinkedIn browser session.

LinkedIn's bot detection does not only look at *how often* you act — it looks
at the shape of the timing and at what the page sees between actions. The
original port had three tells:

* ``random.randint`` produced whole-second delays. Real interaction never
  lands on an exact second boundary.
* A uniform distribution over 8-25s. Human inter-action times are
  right-skewed: mostly quick, occasionally long. Uniform is a machine
  signature even when the range looks generous.
* Fixed ``wait_for_timeout(2500)`` after every navigation, and no page
  activity at all — a profile would open and a click would fire with no
  scrolling, no pointer movement, nothing in between.

This module fixes all three. Everything is jittered from a log-normal-ish
distribution, and :func:`browse_profile` performs the reading behaviour a
person exhibits before acting: scroll down, pause, sometimes scroll back.

Nothing here defeats a determined fingerprinting system. It removes the
*obvious* statistical tells; the daily caps remain the real protection.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# Inter-profile pacing. The mean sits well below the max: most gaps are
# short, with an occasional long one, matching how a person actually works
# through a list.
PROFILE_GAP_MIN = 12.0
PROFILE_GAP_MAX = 90.0
PROFILE_GAP_MEDIAN = 26.0

# Roughly every seventh profile, take a longer break — someone looked away,
# answered a message, got coffee.
LONG_BREAK_PROBABILITY = 0.15
LONG_BREAK_MIN = 90.0
LONG_BREAK_MAX = 240.0


def jitter(base: float, spread: float = 0.35) -> float:
    """Return *base* perturbed by ±*spread*, never below 100 ms.

    Used to break up the fixed waits that follow navigation and clicks, so
    the same step does not take the same number of milliseconds every time.
    """
    factor = 1.0 + random.uniform(-spread, spread)
    return max(0.1, base * factor)


def human_gap(
    minimum: float = PROFILE_GAP_MIN,
    maximum: float = PROFILE_GAP_MAX,
    median: float = PROFILE_GAP_MEDIAN,
) -> float:
    """Sample a right-skewed delay in seconds.

    Log-normal rather than uniform: clustered near the median with a long
    tail, which is what human inter-action gaps look like. A uniform draw
    over the same range is trivially distinguishable from real behaviour.
    """
    # sigma controls tail weight; 0.5 gives a realistic spread without
    # producing absurd outliers once clamped.
    value = random.lognormvariate(0.0, 0.5) * median

    # Clamping to a constant would pile ~6% of draws onto the exact bound,
    # recreating in miniature the repeated-value tell this function exists to
    # avoid. Fold out-of-range draws into a small random band instead.
    if value < minimum:
        return minimum + random.uniform(0.0, min(3.0, (maximum - minimum) * 0.05))
    if value > maximum:
        return maximum - random.uniform(0.0, min(5.0, (maximum - minimum) * 0.05))
    return value


async def pause(seconds: float) -> float:
    """Sleep for a fractional number of seconds."""
    await asyncio.sleep(seconds)
    return seconds


async def settle(page, base_ms: int) -> None:
    """Wait after a navigation or click, with the duration jittered.

    Replaces bare ``page.wait_for_timeout(base_ms)`` so repeated steps do not
    share an identical, machine-perfect duration.
    """
    await page.wait_for_timeout(int(jitter(base_ms)))


async def wander(page, moves: int = 2) -> None:
    """Move the pointer along a few short, irregular hops.

    A session that never emits a pointer event while clicking precise
    coordinates is an obvious tell. Best-effort: failures are ignored, since
    losing mouse jitter must never fail a send.
    """
    try:
        width = await page.evaluate("() => window.innerWidth || 1280")
        height = await page.evaluate("() => window.innerHeight || 800")
    except Exception:
        return

    for _ in range(moves):
        try:
            await page.mouse.move(
                random.randint(int(width * 0.15), int(width * 0.85)),
                random.randint(int(height * 0.15), int(height * 0.85)),
                steps=random.randint(6, 18),
            )
            await asyncio.sleep(random.uniform(0.08, 0.4))
        except Exception:
            return


async def read_page(page, intensity: float = 1.0) -> None:
    """Simulate skimming the page before acting.

    Scrolls down in a few irregular steps with pauses, and sometimes scrolls
    back up — people re-read. ``intensity`` scales the effort: use a lower
    value for pages that are only being checked, higher when the flow is
    about to act on the content.
    """
    try:
        steps = max(1, int(random.randint(2, 4) * intensity))
        for _ in range(steps):
            await page.mouse.wheel(0, random.randint(200, 700))
            await asyncio.sleep(random.uniform(0.4, 1.6))

        # People scroll back up to re-read a headline before deciding.
        if random.random() < 0.35:
            await page.mouse.wheel(0, -random.randint(150, 450))
            await asyncio.sleep(random.uniform(0.3, 1.1))
    except Exception:
        return


async def before_action(page) -> None:
    """The beat between arriving somewhere and acting on it.

    A person reads the profile, moves the pointer, then clicks. Firing a
    click milliseconds after load is one of the cheapest bot signals to
    detect.
    """
    await read_page(page)
    await wander(page)
    await pause(random.uniform(0.6, 2.4))


async def between_profiles(index: int, total: int) -> float:
    """Idle between two profiles, occasionally taking a longer break.

    Returns the seconds waited so the caller can log the cadence.
    """
    if random.random() < LONG_BREAK_PROBABILITY:
        seconds = random.uniform(LONG_BREAK_MIN, LONG_BREAK_MAX)
        logger.info(
            "linkedin: long break %.0fs after profile %d/%d", seconds, index + 1, total
        )
    else:
        seconds = human_gap()
        logger.info(
            "linkedin: waiting %.1fs before profile %d/%d", seconds, index + 2, total
        )
    return await pause(seconds)


async def type_like_human(locator, text: str) -> None:
    """Type with per-character delays instead of setting the value at once.

    ``fill()`` drops the entire string in a single event, which no keyboard
    can produce. Pauses lengthen slightly after sentence punctuation.
    """
    await locator.click(timeout=5000, force=True)
    for char in text:
        await locator.press(char if char != "\n" else "Enter", delay=random.uniform(20, 95))
        if char in ".!?":
            await asyncio.sleep(random.uniform(0.15, 0.5))
        elif char == ",":
            await asyncio.sleep(random.uniform(0.05, 0.2))
