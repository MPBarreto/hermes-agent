"""LinkedIn outbound automation.

Ported from the standalone `linkedin-lead-bot` project, trimmed to the
actuator layer: the browser session, connection invitations, acceptance
detection, and DMs. Lead discovery and lead storage are deliberately absent
— HubSpot is the source of truth for who to contact and what state they're
in, and Apify (via the `apify_sdr` MCP) already handles discovery.

Nothing in this package imports Playwright at module scope. The tool module
is imported by `discover_builtin_tools()` in every Hermes session, including
sessions that will never touch LinkedIn, so the browser dependency is
resolved lazily inside the functions that actually drive a page.
"""
