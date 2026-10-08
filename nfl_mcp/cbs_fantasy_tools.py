"""
CBS Fantasy Football tools for the NFL MCP Server.

This module contains MCP tools for fetching CBS Fantasy Football content including
player news, projections, and expert picks.
"""

import logging
import re

from bs4 import BeautifulSoup

from .config import create_http_client, get_http_headers, validate_limit
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .player_values import scoring_to_ppr
from .week_context import infer_from_calendar

logger = logging.getLogger(__name__)

# CBS concatenates the column abbreviation with its full label in one cell,
# e.g. "attRushing Attempts", "yds/gYards Per Game" or, for kickers,
# "1-19Field Goals 1-19 Yards" and "50+Field Goals 50+ Yards". The abbreviation
# alone is not unique ("yds" is both rushing and receiving yards), so keep the
# descriptive part as the key. Anchored on the following capital so headers that
# are already plain ("Player", "Team") are left untouched.
_HEADER_ABBREV_PREFIX = re.compile(r'^[a-z0-9][a-z0-9/%.+-]*(?=[A-Z])')


def _clean_header(text: str) -> str:
    """Strip CBS's leading column abbreviation from a header label."""
    return _HEADER_ABBREV_PREFIX.sub('', text).strip() or text


@handle_http_errors(
    default_data={"news": [], "total_news": 0},
    operation_name="fetching CBS player news"
)
async def get_cbs_player_news(limit: int | None = 50) -> dict:
    """
    Get the latest fantasy football player news from CBS Sports.

    Parses CBS's player-news list (``ul.player-news-by-sport``; the same parser
    as the ``news`` refresh, `news_sources.parse_cbs`). CBS serves only the
    first page (ten items) to non-browser clients -- deeper pages and the
    per-position pages answer 406 -- so for a player's history use
    ``get_player_news``, which merges ESPN's fantasy feed, NBC Sports /
    Rotoworld and these items.

    Args:
        limit: Maximum number of news items to retrieve (default: 50, max: 100)

    Returns:
        A dictionary containing:
        - news: [{player, position, team, headline, description, published,
          url}] (``published`` is approximate: CBS says "2H ago")
        - total_news: Number of news items returned
        - success / error / error_type
    """
    from .news_sources import CBS_NEWS_URL, parse_cbs

    limit = validate_limit(limit, 1, 100, 50)
    headers = get_http_headers("cbs_fantasy")
    async with create_http_client() as client:
        response = await client.get(CBS_NEWS_URL, headers=headers, follow_redirects=True)
        response.raise_for_status()
        items = parse_cbs(response.text)

    processed_news = [{
        "player": it.get("name"),
        "position": it.get("position"),
        "team": it.get("team"),
        "headline": it.get("title") or it.get("headline"),
        "description": " ".join(p for p in (it.get("headline"), it.get("text")) if p),
        "published": it.get("published_at"),
        "url": it.get("url"),
    } for it in items[:limit]]
    return create_success_response({
        "news": processed_news,
        "total_news": len(processed_news),
        "source": "CBS Sports Fantasy Football",
    })


@handle_http_errors(
    default_data={"projections": [], "total_projections": 0, "week": None, "position": None},
    operation_name="fetching CBS projections"
)
async def get_cbs_projections(
    position: str = "QB",
    week: int | None = None,
    season: int | None = None,
    scoring: str = "ppr"
) -> dict:
    """
    Get fantasy football projections from CBS Sports.

    This tool fetches player projections from CBS Sports Fantasy Football for a specific
    position, week, and scoring format.

    Args:
        position: Player position (QB, RB, WR, TE, K, DST) (default: QB)
        week: NFL week number (1-18, required)
        season: Season year (default: the current NFL season)
        scoring: Scoring format - ppr, half-ppr, standard (default: ppr)

    Note:
        CBS only publishes *season-long* projections at this endpoint — the week
        segment in the URL is ignored server-side (week 1, week 2 and
        "restofseason" all return identical full-season numbers). The returned
        values are therefore labelled ``period: "season"``; do not read them as
        single-week projections.

    Returns:
        A dictionary containing:
        - projections: List of player projections with stats (season totals)
        - total_projections: Number of projections returned
        - week: Week number that was requested (echoed back, not honoured by CBS)
        - period: Always "season" — the granularity of the returned numbers
        - week_honoured: Always False, see Note above
        - position: Position filtered
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate position
    valid_positions = ['QB', 'RB', 'WR', 'TE', 'K', 'DST']
    position = position.upper()
    if position not in valid_positions:
        return handle_validation_error(
            f"Position must be one of: {', '.join(valid_positions)}",
            {"projections": [], "total_projections": 0, "week": week, "position": position}
        )

    # Validate week
    if week is None:
        return handle_validation_error(
            "Week parameter is required (1-18)",
            {"projections": [], "total_projections": 0, "week": week, "position": position}
        )

    if not isinstance(week, int) or week < 1 or week > 18:
        return handle_validation_error(
            "Week must be between 1 and 18",
            {"projections": [], "total_projections": 0, "week": week, "position": position}
        )

    # Validate season
    current_season = infer_from_calendar()[0]
    season = season or current_season
    if season < 2020 or season > current_season + 1:
        season = current_season

    # Scoring format: every spelling the rest of the server accepts
    # ("half_ppr", "0.5", "non-ppr") maps onto CBS's three URL segments; only
    # an unrecognised label falls back to ppr.
    scoring = {0.5: 'half-ppr', 0.0: 'standard'}.get(scoring_to_ppr(scoring), 'ppr')

    headers = get_http_headers("cbs_fantasy")

    # Build CBS projections URL
    url = f"https://www.cbssports.com/fantasy/football/stats/{position}/{season}/{week}/projections/{scoring}/"

    async with create_http_client() as client:
        # Fetch the projections page
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Parse HTML content
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract projection data
        processed_projections = []

        # Look for stats table. CBS renders it as <table class="TableBase-table">;
        # older markup used stats/data/projections classes. Fall back to the only
        # table on the page so a further rename degrades to "wrong table" rather
        # than a silent empty result.
        table = soup.find(
            'table',
            class_=re.compile(r'stats|data|projections|TableBase', re.I)
        )
        if table is None:
            table = soup.find('table')

        if table:
            # Map column names from the header row. CBS uses a two-row thead:
            # the first row holds group spans (Rushing/Receiving/Misc), the
            # second the actual per-column labels. Only the last row lines up
            # with the body cells, so flattening both would misname every value.
            header_row = table.find('thead')
            headers_list = []
            if header_row:
                header_rows = header_row.find_all('tr')
                cells_source = header_rows[-1] if header_rows else header_row
                headers_list = [
                    _clean_header(th.get_text(strip=True))
                    for th in cells_source.find_all(['th', 'td'])
                ]

            # Find data rows
            tbody = table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')

                for row in rows:
                    cells = row.find_all(['td', 'th'])
                    if len(cells) >= 2:
                        projection = {}

                        # First cell holds the player (or, for DST, the team).
                        # It may lead with a text-less logo anchor, so take the
                        # first anchor that actually carries a label — keying off
                        # find('a') alone drops every DST row.
                        player_cell = cells[0]
                        player_link = next(
                            (a for a in player_cell.find_all('a') if a.get_text(strip=True)),
                            None
                        )
                        if player_link:
                            projection['player_name'] = player_link.get_text(strip=True)
                            projection['player_url'] = player_link.get('href')
                        else:
                            projection['player_name'] = player_cell.get_text(strip=True)

                        # Map remaining cells to headers
                        for i, cell in enumerate(cells[1:], start=1):
                            if i < len(headers_list):
                                header_name = headers_list[i]
                                cell_value = cell.get_text(strip=True)
                                # Try to convert to number if possible
                                try:
                                    if '.' in cell_value:
                                        projection[header_name] = float(cell_value)
                                    else:
                                        projection[header_name] = int(cell_value)
                                except (ValueError, AttributeError):
                                    projection[header_name] = cell_value

                        if projection.get('player_name'):
                            processed_projections.append(projection)

        if not processed_projections:
            logger.warning(
                "[CBS] No projections parsed for %s %s week %s — CBS markup may "
                "have changed again (table found: %s)",
                season, position, week, table is not None
            )

        return create_success_response({
            "projections": processed_projections,
            "total_projections": len(processed_projections),
            "week": week,
            # CBS serves identical season-long numbers for every week segment,
            # so be explicit that these are not single-week projections.
            "period": "season",
            "week_honoured": False,
            "position": position,
            "season": season,
            "scoring": scoring,
            "source": "CBS Sports Fantasy Football",
            "note": (
                "CBS publishes season-long projections only; the requested week "
                f"({week}) is not honoured by the source. Values are {season} "
                "full-season totals, not week-level projections."
            )
        })


@handle_http_errors(
    default_data={"picks": [], "total_picks": 0, "week": None},
    operation_name="fetching CBS expert picks"
)
async def get_cbs_expert_picks(week: int | None = None) -> dict:
    """
    Get NFL expert picks against the spread from CBS Sports.

    This tool fetches expert picks from CBS Sports for a specific week,
    providing insights for fantasy and betting decisions.

    Args:
        week: NFL week number (1-18, required)

    Returns:
        A dictionary containing:
        - picks: List of expert picks with game matchups and predictions
        - total_picks: Number of picks returned
        - week: Week number
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate week
    if week is None:
        return handle_validation_error(
            "Week parameter is required (1-18)",
            {"picks": [], "total_picks": 0, "week": week}
        )

    if not isinstance(week, int) or week < 1 or week > 18:
        return handle_validation_error(
            "Week must be between 1 and 18",
            {"picks": [], "total_picks": 0, "week": week}
        )

    headers = get_http_headers("cbs_fantasy")

    # Build CBS expert picks URL
    url = f"https://www.cbssports.com/nfl/picks/experts/against-the-spread/{week}/"

    async with create_http_client() as client:
        # Fetch the expert picks page
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # CBS renders one `TableExpertPicks` table: row 0 = expert names (header),
        # row 1 = their records, rows 2+ = one game each (first cell = matchup,
        # remaining cells = each expert's pick for that game).
        soup = BeautifulSoup(response.text, 'html.parser')
        processed_picks = []
        experts: list[str] = []

        table = (soup.find('table', class_=re.compile(r'TableExpertPicks', re.I))
                 or soup.find('table', class_=re.compile(r'picks', re.I)))
        if table:
            rows = table.find_all('tr')
            header_cells = rows[0].find_all(['th', 'td']) if rows else []
            # First column is the matchup column; the rest are the experts.
            for c in header_cells[1:]:
                name = c.get_text(' ', strip=True)
                # Drop a trailing role/title glued onto the name.
                name = re.sub(r'\s+(Senior|Writer|Analyst|NFL|Fantasy|Editor|Insider).*$', '', name).strip()
                experts.append(name or c.get_text(' ', strip=True))

            _skip_codes = {'CBS', 'NBC', 'FOX', 'ESPN', 'NFL', 'TNF', 'SNF', 'MNF',
                           'AM', 'PM', 'ET', 'FINAL', 'BYE'}
            for row in rows[1:]:
                cells = row.find_all(['th', 'td'])
                if len(cells) < 2:
                    continue
                matchup = re.sub(r'\s+', ' ', cells[0].get_text(' ', strip=True))
                if not matchup or matchup.lower().startswith('week'):
                    continue  # skip the records row / empty rows
                teams = [t for t in re.findall(r'\b[A-Z]{2,3}\b', matchup) if t not in _skip_codes]
                picks_by_expert = {}
                for i, cell in enumerate(cells[1:]):
                    txt = re.sub(r'\s+', ' ', cell.get_text(' ', strip=True))
                    if i < len(experts) and txt:
                        picks_by_expert[experts[i]] = txt
                processed_picks.append({
                    "matchup": matchup,
                    "away_team": teams[0] if len(teams) >= 1 else None,
                    "home_team": teams[1] if len(teams) >= 2 else None,
                    "picks": picks_by_expert,
                })

        return create_success_response({
            "picks": processed_picks,
            "total_picks": len(processed_picks),
            "experts": experts,
            "week": week,
            "source": "CBS Sports Expert Picks"
        })
