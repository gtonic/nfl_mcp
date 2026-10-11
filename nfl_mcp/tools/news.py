"""Player news (ESPN / NBC / CBS feeds), CBS projections and expert picks, URL crawl.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from .. import (
    cbs_fantasy_tools,
    player_news_tools,
    web_tools,
)
from ..config import (
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from ._context import _current_season_week

# =============================================================================
# CBS FANTASY TOOLS
# =============================================================================

@timing_decorator("get_player_news", tool_type="nfl")
async def get_player_news(
    players: list[str] | None = None,
    days: int | None = 7,
    league_id: str | None = None,
) -> dict:
    """Player news timeline + role/availability flags ("what is the latest on X?").

    One merged, deduplicated timeline per player from the stored news of
    every source -- ESPN's fantasy player feed (RotoWire notes), NBC Sports /
    Rotoworld, CBS -- plus his current injury blurb. A note several sources
    carry is one entry (`sources` lists each copy with its link). Also the
    flags the projections read from that text (benched, committee, lead_role,
    limited_snaps, week_to_week, designated_to_return, practice_progress,
    expected_to_play, unlikely_to_play, ruled_out, inactive_healthy_scratch),
    each with source, url and snippet; a teammate's note that names him
    counts for him. A flag inside a condition ("would be the lead back if
    Hall can't go") is `conditional` (a timeline entry shows it as
    "lead_role?"). Reads the store only: refresh it with
    refresh_data(scope=["news"]) (the prefetch loop polls every 45 min, every
    15 min on game days). `sources` reports each source's health (ok /
    degraded / failing, why, failure streak, backoff) and `warnings` names a
    source that is not ok -- its items are missing until it recovers. A
    source is switched off with one env line on the server, e.g.
    NFL_MCP_NEWS_SOURCES=-nbc (all but NBC), =espn_fantasy (ESPN only), =none.

    Parameters:
        players: names or Sleeper ids (up to 30), e.g. ["Lamar Jackson", "4881"]
        days (int, default 7, 1-30): timeline window
        league_id: Sleeper league; a shared name resolves to the player rostered
            there, and without `players` every rostered QB/RB/WR/TE/K with news
            is listed (most flags first, 4 entries each)

    Returns: {
        players [{query, name, player_id, team, position, injury_status, items,
                  news_flags [{flag, weight, snippet, date_reported, source, url,
                  from_player?, conditional?}], news_adjustment {model_mult, confidence_delta,
                  applied}?, timeline [{published_at, headline, text, source,
                  url, sources [{source, url}], flags}], ambiguous?, candidates?}],
        unresolved [{name, reason}], window_days,
        sources {source: {label, fetched_at, age_hours, status, health,
                 last_success_at, newest_item, detail?, consecutive_failures?,
                 next_attempt_at?, enabled?, error?}},
        notes, warnings?, success
    }

    Example: get_player_news(players=["Lamar Jackson", "D'Andre Swift"], days=7)
    Example: get_player_news(league_id="1388610560915959808")
    """
    if isinstance(players, str):
        players = [players]
    players = [validate_string_input(p, 'player', max_length=100, required=True)
               for p in (players or []) if isinstance(p, str) and p.strip()]
    if league_id is not None:
        league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    days_i = validate_numeric_input(days, min_val=1, max_val=30, default=7, required=False)
    return await player_news_tools.get_player_news(players=players, days=days_i,
                                                   league_id=league_id)


@timing_decorator("get_cbs_player_news", tool_type="cbs_fantasy")
async def get_cbs_player_news(limit: int | None = 50) -> dict:
    """Fetch latest fantasy football player news from CBS Sports.

    Parameters:
        limit (int, default 50, range 1-100): Max number of news items.
    Returns: {news: [...], total_news, success, error?}
    Example: get_cbs_player_news(limit=25)
    """
    return await cbs_fantasy_tools.get_cbs_player_news(limit)


@timing_decorator("get_cbs_projections", tool_type="cbs_fantasy")
async def get_cbs_projections(
    position: str = "QB",
    season: int | None = None,
    scoring: str = "ppr",
) -> dict:
    """CBS Sports SEASON-LONG fantasy projections for one position (not weekly).

    CBS publishes only full-season totals at this source — there is no week
    parameter because CBS ignores it (every week returns the same numbers).
    Use it as a season-long outside opinion; for this week's points use
    project_players, for rest-of-season in your league's scoring
    get_ros_projections.

    Parameters:
        position (str, default "QB"): QB, RB, WR, TE, K or DST.
        season (int, default: the current NFL season): Season year.
        scoring (str, default "ppr"): ppr, half-ppr or standard.
    Returns: {projections: [...], total_projections, period: "season",
        week_honoured: false, position, success, error?}
    Example: get_cbs_projections(position="RB", scoring="half-ppr")
    """
    try:
        season_i = int(season) if season is not None else None
    except Exception:
        season_i = None
    # The source URL carries a week segment CBS ignores; any valid week works.
    result = await cbs_fantasy_tools.get_cbs_projections(
        position=position, week=1, season=season_i, scoring=scoring,
    )
    if isinstance(result, dict):
        result["week"] = None
        result["period"] = "season"
        result["week_honoured"] = False
    return result


@timing_decorator("get_cbs_expert_picks", tool_type="cbs_fantasy")
async def get_cbs_expert_picks(week: int | None = None) -> dict:
    """CBS Sports experts' NFL picks AGAINST THE SPREAD for a week (betting
    picks per game, not fantasy advice).

    Parameters:
        week (int, optional): NFL week 1-18 (default: current week).
    Returns: {picks: [...], total_picks, week, success, error?}
    Example: get_cbs_expert_picks(week=3)
    """
    try:
        week_i = int(week) if week is not None else None
    except Exception:
        week_i = None
    if week_i is None:
        _, week_i = await _current_season_week()
    return await cbs_fantasy_tools.get_cbs_expert_picks(week=week_i)


# =============================================================================
# WEB TOOLS
# =============================================================================

@timing_decorator("crawl_url", tool_type="web")
async def crawl_url(url: str, max_length: int | None = 10000) -> dict:
    """Crawl URL and extract text content for LLM processing.

    Parameters:
        url (str, required): The URL to crawl (must include http:// or https://).
        max_length (int, default 10000, range 100-50000): Maximum length of extracted text.
    Returns: {url, title, content, content_length, success, error?}
    Example: crawl_url(url="https://example.com", max_length=5000)
    """
    return await web_tools.crawl_url(url, max_length)
