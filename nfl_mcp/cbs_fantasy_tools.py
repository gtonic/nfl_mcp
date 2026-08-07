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

logger = logging.getLogger(__name__)


@handle_http_errors(
    default_data={"news": [], "total_news": 0},
    operation_name="fetching CBS player news"
)
async def get_cbs_player_news(limit: int | None = 50) -> dict:
    """
    Get the latest fantasy football player news from CBS Sports.

    This tool fetches current player news from CBS Sports Fantasy Football section
    and returns them in a structured format suitable for LLM processing.

    Args:
        limit: Maximum number of news items to retrieve (default: 50, max: 100)

    Returns:
        A dictionary containing:
        - news: List of player news items with headlines, players, descriptions
        - total_news: Number of news items returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate and cap the limit
    limit = validate_limit(
        limit,
        1,
        100,
        50
    )

    headers = get_http_headers("cbs_fantasy")

    # CBS Fantasy player news URL
    url = "https://www.cbssports.com/fantasy/football/players/news/all/"

    async with create_http_client() as client:
        # Fetch the news page from CBS Sports
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        # Parse HTML content
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract news items from the page
        # CBS uses various structures, so we'll try multiple selectors
        processed_news = []

        # Look for news containers - common patterns in sports sites
        news_containers = (
            soup.find_all('article', class_=re.compile(r'player.*news|news.*item|article.*item', re.I)) or
            soup.find_all('div', class_=re.compile(r'player.*news|news.*item|article.*item', re.I)) or
            soup.find_all('div', class_=re.compile(r'news.*card|card.*news', re.I))
        )

        for container in news_containers[:limit]:
            news_item = {}

            # Extract player name
            player_elem = (
                container.find(['a', 'span', 'h3', 'h4'], class_=re.compile(r'player.*name', re.I)) or
                container.find(['a', 'span', 'h3', 'h4'], attrs={'data-player': True})
            )
            if player_elem:
                news_item['player'] = player_elem.get_text(strip=True)

            # Extract headline/title
            headline_elem = (
                container.find(['h2', 'h3', 'h4', 'a'], class_=re.compile(r'headline|title', re.I)) or
                container.find(['h2', 'h3', 'h4'])
            )
            if headline_elem:
                news_item['headline'] = headline_elem.get_text(strip=True)

            # Extract description/summary
            desc_elem = (
                container.find(['p', 'div'], class_=re.compile(r'description|summary|excerpt|content', re.I)) or
                container.find('p')
            )
            if desc_elem:
                news_item['description'] = desc_elem.get_text(strip=True)

            # Extract timestamp if available
            time_elem = (
                container.find('time') or
                container.find(['span', 'div'], class_=re.compile(r'date|time|timestamp', re.I))
            )
            if time_elem:
                news_item['published'] = time_elem.get('datetime') or time_elem.get_text(strip=True)

            # Extract position if available
            position_elem = container.find(['span', 'div'], class_=re.compile(r'position|pos', re.I))
            if position_elem:
                news_item['position'] = position_elem.get_text(strip=True)

            # Extract team if available
            team_elem = container.find(['span', 'div', 'a'], class_=re.compile(r'team', re.I))
            if team_elem:
                news_item['team'] = team_elem.get_text(strip=True)

            # Only add if we have at least a headline or description
            if news_item.get('headline') or news_item.get('description'):
                processed_news.append(news_item)

        return create_success_response({
            "news": processed_news,
            "total_news": len(processed_news),
            "source": "CBS Sports Fantasy Football"
        })


@handle_http_errors(
    default_data={"projections": [], "total_projections": 0, "week": None, "position": None},
    operation_name="fetching CBS projections"
)
async def get_cbs_projections(
    position: str = "QB",
    week: int | None = None,
    season: int | None = 2026,
    scoring: str = "ppr"
) -> dict:
    """
    Get fantasy football projections from CBS Sports.

    This tool fetches player projections from CBS Sports Fantasy Football for a specific
    position, week, and scoring format.

    Args:
        position: Player position (QB, RB, WR, TE, K, DST) (default: QB)
        week: NFL week number (1-18, required)
        season: Season year (default: 2026)
        scoring: Scoring format - ppr, half-ppr, standard (default: ppr)

    Returns:
        A dictionary containing:
        - projections: List of player projections with stats
        - total_projections: Number of projections returned
        - week: Week number
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
    season = season or 2026
    if season < 2020 or season > 2030:
        season = 2026

    # Validate scoring format
    valid_scoring = ['ppr', 'half-ppr', 'standard']
    scoring = scoring.lower()
    if scoring not in valid_scoring:
        scoring = 'ppr'

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

        # Look for stats table - common in sports sites
        table = soup.find('table', class_=re.compile(r'stats|data|projections', re.I))

        if table:
            # Find header row to map column names
            header_row = table.find('thead')
            headers_list = []
            if header_row:
                headers_list = [th.get_text(strip=True) for th in header_row.find_all(['th', 'td'])]

            # Find data rows
            tbody = table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')

                for row in rows:
                    cells = row.find_all(['td', 'th'])
                    if len(cells) >= 2:
                        projection = {}

                        # First cell usually contains player info
                        player_cell = cells[0]
                        player_link = player_cell.find('a')
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

        return create_success_response({
            "projections": processed_projections,
            "total_projections": len(processed_projections),
            "week": week,
            "position": position,
            "season": season,
            "scoring": scoring,
            "source": "CBS Sports Fantasy Football"
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
