"""
Response validation and schema checks for API responses.

This module provides validation for API responses to ensure data quality
and catch issues early before they cause problems downstream.
"""

import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime

logger = logging.getLogger(__name__)


class ValidationResult:
    """Result of a validation check."""
    
    def __init__(self, valid: bool = True, errors: Optional[List[str]] = None, warnings: Optional[List[str]] = None):
        self.valid = valid
        self.errors = errors or []
        self.warnings = warnings or []
    
    def add_error(self, message: str):
        """Add an error message."""
        self.errors.append(message)
        self.valid = False
    
    def add_warning(self, message: str):
        """Add a warning message."""
        self.warnings.append(message)
    
    def is_valid(self) -> bool:
        """Check if validation passed."""
        return self.valid
    
    def __str__(self) -> str:
        """String representation."""
        if self.valid:
            result = "Valid"
            if self.warnings:
                result += f" (with {len(self.warnings)} warnings)"
            return result
        return f"Invalid: {', '.join(self.errors)}"


def validate_snap_count_response(data: Dict[str, Any]) -> ValidationResult:
    """
    Validate snap count response from Sleeper API.
    
    Expected format:
    {
        "player_id": {
            "snaps": int,
            "snap_pct": float,
            ...
        }
    }
    
    Args:
        data: Response data from API
        
    Returns:
        ValidationResult with validation status
    """
    result = ValidationResult()
    
    if not isinstance(data, dict):
        result.add_error("Response must be a dictionary")
        return result
    
    if len(data) == 0:
        result.add_warning("Response contains no player data")
        return result
    
    # Sample first few entries to check structure
    sample_size = min(10, len(data))
    sample_items = list(data.items())[:sample_size]
    
    players_with_snaps = 0
    players_with_snap_pct = 0
    
    for player_id, stats in sample_items:
        if not isinstance(stats, dict):
            result.add_error(f"Player {player_id} stats must be a dictionary")
            continue
        
        # Check for snap data (Sleeper API uses off_snp, team_snp, def_snp, st_snp)
        has_snaps = any(key in stats for key in ['snaps', 'off_snaps', 'offense_snaps', 'off_snp', 'team_snp', 'def_snp', 'st_snp'])
        has_snap_pct = any(key in stats for key in ['snap_pct', 'off_snap_pct', 'off_snp_pct', 'snap_share', 'snap_pct_formatted'])
        
        if has_snaps:
            players_with_snaps += 1
        if has_snap_pct:
            players_with_snap_pct += 1
    
    # Warn if low percentage of players have snap data
    if sample_size > 0:
        snap_coverage = (players_with_snaps / sample_size) * 100
        snap_pct_coverage = (players_with_snap_pct / sample_size) * 100
        
        if snap_coverage < 30:
            result.add_warning(f"Low snap data coverage: {snap_coverage:.1f}% of sampled players")
        
        if snap_pct_coverage < 30:
            result.add_warning(f"Low snap_pct coverage: {snap_pct_coverage:.1f}% of sampled players")
    
    logger.debug(
        f"[Validate Snaps] {len(data)} players, "
        f"{players_with_snaps}/{sample_size} with snaps, "
        f"{players_with_snap_pct}/{sample_size} with snap_pct"
    )
    
    return result


def validate_schedule_response(games: List[Dict[str, Any]]) -> ValidationResult:
    """
    Validate schedule response from ESPN API.
    
    Expected format: List of game dictionaries with team, opponent, etc.
    
    Args:
        games: List of game records
        
    Returns:
        ValidationResult with validation status
    """
    result = ValidationResult()
    
    if not isinstance(games, list):
        result.add_error("Schedule must be a list")
        return result
    
    if len(games) == 0:
        result.add_warning("Schedule contains no games")
        return result
    
    required_fields = ['season', 'week', 'team', 'opponent']
    games_missing_fields = 0
    
    for idx, game in enumerate(games[:20]):  # Sample first 20
        if not isinstance(game, dict):
            result.add_error(f"Game {idx} must be a dictionary")
            continue
        
        missing = [field for field in required_fields if field not in game]
        if missing:
            games_missing_fields += 1
            if games_missing_fields <= 3:  # Only report first few
                result.add_error(f"Game {idx} missing fields: {', '.join(missing)}")
    
    if games_missing_fields > 0:
        result.add_warning(f"{games_missing_fields} games have missing fields")
    
    logger.debug(f"[Validate Schedule] {len(games)} games validated")
    
    return result


def validate_practice_report_response(reports: List[Dict[str, Any]]) -> ValidationResult:
    """
    Validate practice report response.
    
    Expected format: List of practice status records with player_id, status, date.
    
    Args:
        reports: List of practice status records
        
    Returns:
        ValidationResult with validation status
    """
    result = ValidationResult()
    
    if not isinstance(reports, list):
        result.add_error("Practice reports must be a list")
        return result
    
    if len(reports) == 0:
        result.add_warning("No practice reports available")
        return result
    
    required_fields = ['player_id', 'status']
    valid_statuses = {'DNP', 'LP', 'FP', 'OUT', 'QUESTIONABLE', 'DOUBTFUL'}
    
    reports_missing_fields = 0
    reports_invalid_status = 0
    
    for idx, report in enumerate(reports[:20]):  # Sample first 20
        if not isinstance(report, dict):
            result.add_error(f"Report {idx} must be a dictionary")
            continue
        
        missing = [field for field in required_fields if field not in report]
        if missing:
            reports_missing_fields += 1
            if reports_missing_fields <= 3:
                result.add_error(f"Report {idx} missing fields: {', '.join(missing)}")
        
        status = report.get('status', '').upper()
        if status and status not in valid_statuses:
            reports_invalid_status += 1
            if reports_invalid_status <= 3:
                result.add_warning(f"Report {idx} has unusual status: {status}")
    
    if reports_missing_fields > 0:
        result.add_warning(f"{reports_missing_fields} reports have missing fields")
    
    logger.debug(f"[Validate Practice] {len(reports)} reports validated")
    
    return result


def validate_usage_stats_response(stats: List[Dict[str, Any]]) -> ValidationResult:
    """
    Validate usage statistics response.
    
    Expected format: List of usage stat records with player_id, targets, routes, etc.
    
    Args:
        stats: List of usage stat records
        
    Returns:
        ValidationResult with validation status
    """
    result = ValidationResult()
    
    if not isinstance(stats, list):
        result.add_error("Usage stats must be a list")
        return result
    
    if len(stats) == 0:
        result.add_warning("No usage stats available")
        return result
    
    required_fields = ['player_id', 'season', 'week']
    usage_fields = ['targets', 'routes', 'rz_touches', 'touches']
    
    stats_missing_fields = 0
    stats_with_usage = 0
    
    for idx, stat in enumerate(stats[:20]):  # Sample first 20
        if not isinstance(stat, dict):
            result.add_error(f"Stat {idx} must be a dictionary")
            continue
        
        missing = [field for field in required_fields if field not in stat]
        if missing:
            stats_missing_fields += 1
            if stats_missing_fields <= 3:
                result.add_error(f"Stat {idx} missing fields: {', '.join(missing)}")
        
        # Check if at least one usage metric is present
        has_usage = any(stat.get(field) is not None for field in usage_fields)
        if has_usage:
            stats_with_usage += 1
    
    if stats_missing_fields > 0:
        result.add_warning(f"{stats_missing_fields} stats have missing fields")
    
    # Warn if low percentage have usage data
    sample_size = min(20, len(stats))
    if sample_size > 0 and stats_with_usage < sample_size * 0.5:
        result.add_warning(
            f"Low usage data coverage: {stats_with_usage}/{sample_size} "
            f"({100*stats_with_usage/sample_size:.1f}%) have usage metrics"
        )
    
    logger.debug(
        f"[Validate Usage] {len(stats)} stats validated, "
        f"{stats_with_usage}/{sample_size} with usage data"
    )
    
    return result


def validate_response_and_log(
    data: Any,
    validator_func,
    data_type: str,
    allow_partial: bool = True
) -> bool:
    """
    Validate response and log results.
    
    Args:
        data: Data to validate
        validator_func: Validation function to use
        data_type: Type of data (for logging)
        allow_partial: Whether to allow partial data on validation warnings
        
    Returns:
        True if data is usable (valid or partial with warnings), False otherwise
    """
    result = validator_func(data)
    
    if result.is_valid():
        if result.warnings:
            for warning in result.warnings:
                logger.warning(f"[Validate {data_type}] {warning}")
            if allow_partial:
                logger.info(f"[Validate {data_type}] Accepting partial data with warnings")
                return True
        else:
            logger.debug(f"[Validate {data_type}] Validation passed")
        return True
    else:
        for error in result.errors:
            logger.error(f"[Validate {data_type}] {error}")
        logger.error(f"[Validate {data_type}] Validation failed, rejecting data")
        return False
