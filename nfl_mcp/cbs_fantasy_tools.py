"""
CBS Fantasy Football tools for the NFL MCP Server.

This module contains MCP tools for fetching CBS Fantasy Football content including
player news, projections, and expert picks.
"""

import httpx
from typing import Optional, Dict, Any, List
import logging
from bs4 import BeautifulSoup
import re

from .config import get_http_headers, create_http_client, validate_limit, LIMITS
from .errors import (
    create_error_response, create_success_response, ErrorType,
    handle_http_errors, handle_validation_error
)

logger = logging.getLogger(__name__)


@handle_http_errors(
    default_data={"news": [], "total_news": 0},
    operation_name="fetching CBS player news"
)
async def get_cbs_player_news(limit: Optional[int] = 50) -> dict:
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
    week: Optional[int] = None,
    season: Optional[int] = 2025,
    scoring: str = "ppr"
) -> dict:
    """
    Get fantasy football projections from CBS Sports.
    
    This tool fetches player projections from CBS Sports Fantasy Football for a specific
    position, week, and scoring format.
    
    Args:
        position: Player position (QB, RB, WR, TE, K, DST) (default: QB)
        week: NFL week number (1-18, required)
        season: Season year (default: 2025)
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
    season = season or 2025
    if season < 2020 or season > 2030:
        season = 2025
    
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
async def get_cbs_expert_picks(week: Optional[int] = None) -> dict:
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
        
        # Parse HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract picks data
        processed_picks = []
        
        # Look for picks table or containers
        picks_table = soup.find('table', class_=re.compile(r'picks|games|matchup', re.I))
        
        if picks_table:
            # Find data rows
            rows = picks_table.find_all('tr')
            
            for row in rows[1:]:  # Skip header row
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 3:
                    pick = {}
                    
                    # Extract game/matchup info (usually first cell)
                    matchup_cell = cells[0]
                    pick['matchup'] = matchup_cell.get_text(strip=True)
                    
                    # Extract teams if available
                    team_links = matchup_cell.find_all('a')
                    if len(team_links) >= 2:
                        pick['away_team'] = team_links[0].get_text(strip=True)
                        pick['home_team'] = team_links[1].get_text(strip=True)
                    
                    # Extract expert picks from remaining cells
                    pick['experts'] = []
                    for cell in cells[1:]:
                        expert_pick = cell.get_text(strip=True)
                        if expert_pick:
                            pick['experts'].append(expert_pick)
                    
                    if pick.get('matchup'):
                        processed_picks.append(pick)
        else:
            # Alternative: look for individual pick cards/containers
            pick_containers = soup.find_all(['div', 'article'], class_=re.compile(r'pick|game|matchup', re.I))
            
            for container in pick_containers:
                pick = {}
                
                # Extract matchup info
                matchup_elem = container.find(['h3', 'h4', 'div'], class_=re.compile(r'matchup|game|title', re.I))
                if matchup_elem:
                    pick['matchup'] = matchup_elem.get_text(strip=True)
                
                # Extract expert name and pick
                expert_elem = container.find(['span', 'div'], class_=re.compile(r'expert|analyst', re.I))
                if expert_elem:
                    pick['expert'] = expert_elem.get_text(strip=True)
                
                prediction_elem = container.find(['span', 'div'], class_=re.compile(r'pick|prediction', re.I))
                if prediction_elem:
                    pick['prediction'] = prediction_elem.get_text(strip=True)
                
                if pick.get('matchup'):
                    processed_picks.append(pick)
        
        return create_success_response({
            "picks": processed_picks,
            "total_picks": len(processed_picks),
            "week": week,
            "source": "CBS Sports Expert Picks"
        })
