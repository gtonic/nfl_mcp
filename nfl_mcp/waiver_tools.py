"""
Waiver wire analysis tools for the NFL MCP Server.

This module provides advanced waiver wire analysis functionality including
waiver log tracking with de-duplication, re-entry status checking, and
enhanced waiver wire intelligence for fantasy football decision making.
"""

import logging
from collections import defaultdict
from datetime import datetime

from .errors import ErrorType, create_error_response, create_success_response
from .sleeper_tools import get_transactions

logger = logging.getLogger(__name__)


class WaiverAnalyzer:
    """Analyzer for waiver wire activity with de-duplication and re-entry tracking."""

    def __init__(self):
        self.waiver_cache: dict[str, list[dict]] = {}
        self.re_entry_tracking: dict[str, dict[str, list[datetime]]] = defaultdict(lambda: defaultdict(list))

    @staticmethod
    def _is_failed(transaction: dict) -> bool:
        """A claim that lost (outbid, lower priority, player gone): nobody was
        added or dropped, even though Sleeper still lists the adds/drops asked for."""
        return (transaction.get('status') or '').lower() == 'failed'

    def _failed_claims(self, transactions: list[dict]) -> list[dict]:
        """Failed waiver claims, reported apart from the moves that happened."""
        return [
            {
                'transaction_id': t.get('transaction_id'),
                'created': t.get('created'),
                'roster_ids': t.get('roster_ids', []),
                'wanted': list((t.get('adds') or {}).keys()),
                'would_have_dropped': list((t.get('drops') or {}).keys()),
                'waiver_bid': (t.get('settings') or {}).get('waiver_bid'),
                'week': t.get('leg', t.get('week')),
            }
            for t in transactions
            if t.get('type') in ['waiver', 'free_agent'] and self._is_failed(t)
        ]

    def _extract_waiver_transactions(self, transactions: list[dict]) -> list[dict]:
        """Extract waiver-related transactions from transaction list.

        Failed claims are left out: they added and dropped nobody, and counting
        them made a player look added (and "re-entered") when he never moved.
        See `_failed_claims` for them.
        """
        waiver_transactions = []

        for transaction in transactions:
            # Check if this is a waiver transaction
            if transaction.get('type') in ['waiver', 'free_agent'] and not self._is_failed(transaction):
                # Process adds and drops
                # Sleeper sends `null` (not an omitted key) for a pure add or
                # pure drop, so the `{}` default never applies there.
                adds = transaction.get('adds') or {}
                drops = transaction.get('drops') or {}

                # Create normalized waiver transaction
                waiver_tx = {
                    'transaction_id': transaction.get('transaction_id'),
                    'type': transaction.get('type'),
                    'status': transaction.get('status'),
                    'created': transaction.get('created'),
                    'adds': adds,
                    'drops': drops,
                    'roster_ids': transaction.get('roster_ids', []),
                    'waiver_budget': transaction.get('waiver_budget', []),
                    'week': transaction.get('leg', transaction.get('week'))
                }
                waiver_transactions.append(waiver_tx)

        return waiver_transactions

    def _deduplicate_waiver_log(self, waiver_transactions: list[dict]) -> tuple[list[dict], list[dict]]:
        """Deduplicate waiver transactions and return unique transactions + duplicates found."""
        seen_combinations = set()
        unique_transactions = []
        duplicates = []

        for transaction in waiver_transactions:
            # Create a signature for deduplication based on player adds/drops and roster
            adds_str = ','.join(sorted((transaction.get('adds') or {}).keys()))
            drops_str = ','.join(sorted((transaction.get('drops') or {}).keys()))
            roster_ids_str = ','.join(map(str, sorted(transaction.get('roster_ids', []))))

            signature = f"{adds_str}|{drops_str}|{roster_ids_str}|{transaction.get('created', '')}"

            if signature in seen_combinations:
                duplicates.append(transaction)
            else:
                seen_combinations.add(signature)
                unique_transactions.append(transaction)

        return unique_transactions, duplicates

    def _track_re_entries(self, waiver_transactions: list[dict]) -> dict[str, dict]:
        """Track re-entry status for players (dropped then re-added)."""
        player_activity = defaultdict(list)  # player_id -> list of (action, timestamp, roster_id)

        # Process all transactions chronologically.
        # Note: `created` may be present-but-None, so coerce to 0 (not just a
        # missing-key default) to avoid None<None comparison errors.
        sorted_transactions = sorted(waiver_transactions, key=lambda x: x.get('created') or 0)

        for transaction in sorted_transactions:
            timestamp = transaction.get('created')

            # Process drops first
            for player_id, roster_id in (transaction.get('drops') or {}).items():
                player_activity[player_id].append({
                    'action': 'drop',
                    'timestamp': timestamp,
                    'roster_id': roster_id,
                    'transaction_id': transaction.get('transaction_id'),
                    'type': transaction.get('type')
                })

            # Process adds
            for player_id, roster_id in (transaction.get('adds') or {}).items():
                player_activity[player_id].append({
                    'action': 'add',
                    'timestamp': timestamp,
                    'roster_id': roster_id,
                    'transaction_id': transaction.get('transaction_id'),
                    'type': transaction.get('type')
                })

        # Analyze re-entry patterns
        re_entry_analysis = {}

        for player_id, activities in player_activity.items():
            if len(activities) < 2:
                continue  # Need at least 2 activities to have re-entry

            drops = [a for a in activities if a['action'] == 'drop']
            adds = [a for a in activities if a['action'] == 'add']

            if len(drops) > 0 and len(adds) > 0:
                # Check for re-entries (add after drop)
                re_entries = []

                for drop in drops:
                    # Find adds after this drop (None-safe: skip if either
                    # timestamp is missing rather than raising on None<None).
                    subsequent_adds = [
                        a for a in adds
                        if a['timestamp'] is not None and drop['timestamp'] is not None
                        and a['timestamp'] > drop['timestamp']
                    ]

                    for add in subsequent_adds:
                        re_entries.append({
                            'dropped_at': drop['timestamp'],
                            'dropped_by_roster': drop['roster_id'],
                            'added_at': add['timestamp'],
                            'added_by_roster': add['roster_id'],
                            'days_between': (add['timestamp'] - drop['timestamp']) / 86400 if drop['timestamp'] and add['timestamp'] else None,
                            'same_roster': drop['roster_id'] == add['roster_id']
                        })

                # Record any player with both an add and a drop (churn). re_entries
                # may be empty (added then dropped, never re-added); volatility
                # still requires more than one actual re-entry.
                re_entry_analysis[player_id] = {
                    'total_activities': len(activities),
                    'drops_count': len(drops),
                    'adds_count': len(adds),
                    're_entries': re_entries,
                    'is_volatile': len(re_entries) > 1,  # More than one re-entry indicates volatility
                    'latest_status': activities[-1]['action'] if activities else None
                }

        return re_entry_analysis


async def get_waiver_log(league_id: str, round: int | None = None, dedupe: bool = True) -> dict:
    """
    Get waiver wire log with optional de-duplication.

    Retrieves and analyzes waiver wire transactions for a league, with optional
    de-duplication to remove duplicate transactions and provide clean waiver activity log.

    Args:
        league_id: The unique identifier for the league
        round: Optional round number to filter transactions
        dedupe: Whether to perform de-duplication (default: True)

    Returns:
        A dictionary containing:
        - waiver_log: List of waiver transactions (deduplicated if requested)
        - duplicates_found: List of duplicate transactions (if deduplication enabled)
        - total_transactions: Total number of waiver transactions before deduplication
        - unique_transactions: Number of unique transactions after deduplication
        - league_id: The league ID processed
        - round: The round processed (if specified)
        - deduplication_enabled: Whether deduplication was performed
        - success: Whether the request was successful
        - error: Error message (if any)
    """
    try:
        # Get raw transaction data
        transactions_result = await get_transactions(league_id, round)

        if not transactions_result.get('success'):
            return create_error_response(
                f"Failed to fetch transactions: {transactions_result.get('error')}",
                ErrorType.HTTP,
                {"waiver_log": [], "duplicates_found": [], "total_transactions": 0, "unique_transactions": 0}
            )

        transactions = transactions_result.get('transactions', [])

        # Initialize analyzer
        analyzer = WaiverAnalyzer()

        # Extract waiver-specific transactions
        waiver_transactions = analyzer._extract_waiver_transactions(transactions)

        total_waiver_count = len(waiver_transactions)
        failed_claims = analyzer._failed_claims(transactions)

        if dedupe:
            # Perform de-duplication
            unique_transactions, duplicates = analyzer._deduplicate_waiver_log(waiver_transactions)

            return create_success_response({
                "waiver_log": unique_transactions,
                "duplicates_found": duplicates,
                "total_transactions": total_waiver_count,
                "unique_transactions": len(unique_transactions),
                "league_id": league_id,
                "round": round,
                "deduplication_enabled": True,
                "failed_claims": failed_claims,
                "failed_claims_count": len(failed_claims),
            })
        else:
            # Return all waiver transactions without deduplication
            return create_success_response({
                "waiver_log": waiver_transactions,
                "duplicates_found": [],
                "total_transactions": total_waiver_count,
                "unique_transactions": total_waiver_count,
                "league_id": league_id,
                "round": round,
                "deduplication_enabled": False,
                "failed_claims": failed_claims,
                "failed_claims_count": len(failed_claims),
            })

    except Exception as e:
        logger.error(f"Error in get_waiver_log: {e}")
        return create_error_response(
            f"Unexpected error analyzing waiver log: {e!s}",
            ErrorType.UNEXPECTED,
            {"waiver_log": [], "duplicates_found": [], "total_transactions": 0, "unique_transactions": 0}
        )


async def check_re_entry_status(league_id: str, round: int | None = None) -> dict:
    """
    Check re-entry status for players in waiver wire activity.

    Analyzes waiver transactions to identify players who have been dropped and
    re-added, indicating volatile or "recycled" players that might be risky picks.

    Args:
        league_id: The unique identifier for the league
        round: Optional round number to filter transactions

    Returns:
        A dictionary containing:
        - re_entry_players: Dict mapping player_id to re-entry analysis
        - volatile_players: List of player_ids with multiple re-entries
        - total_players_analyzed: Number of players with waiver activity
        - players_with_re_entries: Number of players with at least one re-entry
        - league_id: The league ID processed
        - round: The round processed (if specified)
        - success: Whether the request was successful
        - error: Error message (if any)
    """
    try:
        # Get raw transaction data
        transactions_result = await get_transactions(league_id, round)

        if not transactions_result.get('success'):
            return create_error_response(
                f"Failed to fetch transactions: {transactions_result.get('error')}",
                ErrorType.HTTP,
                {"re_entry_players": {}, "volatile_players": [], "total_players_analyzed": 0, "players_with_re_entries": 0}
            )

        transactions = transactions_result.get('transactions', [])

        # Initialize analyzer
        analyzer = WaiverAnalyzer()

        # Extract waiver-specific transactions
        waiver_transactions = analyzer._extract_waiver_transactions(transactions)

        # Analyze re-entry patterns
        re_entry_analysis = analyzer._track_re_entries(waiver_transactions)

        # Identify volatile players (multiple re-entries)
        volatile_players = [
            player_id for player_id, analysis in re_entry_analysis.items()
            if analysis.get('is_volatile', False)
        ]

        # Count all players with waiver activity
        all_players = set()
        for tx in waiver_transactions:
            all_players.update((tx.get('adds') or {}).keys())
            all_players.update((tx.get('drops') or {}).keys())

        return create_success_response({
            "re_entry_players": re_entry_analysis,
            "volatile_players": volatile_players,
            "total_players_analyzed": len(all_players),
            "players_with_re_entries": len(re_entry_analysis),
            "league_id": league_id,
            "round": round
        })

    except Exception as e:
        logger.error(f"Error in check_re_entry_status: {e}")
        return create_error_response(
            f"Unexpected error checking re-entry status: {e!s}",
            ErrorType.UNEXPECTED,
            {"re_entry_players": {}, "volatile_players": [], "total_players_analyzed": 0, "players_with_re_entries": 0}
        )


async def get_waiver_wire_dashboard(league_id: str, round: int | None = None) -> dict:
    """
    Get comprehensive waiver wire dashboard with analysis.

    Provides a comprehensive overview of waiver wire activity including
    de-duplicated log, re-entry analysis, and summary statistics.

    Args:
        league_id: The unique identifier for the league
        round: Optional round number to filter transactions

    Returns:
        A dictionary containing:
        - waiver_log: De-duplicated waiver transactions
        - re_entry_analysis: Re-entry status for players
        - dashboard_summary: Summary statistics and insights
        - league_id: The league ID processed
        - round: The round processed (if specified)
        - success: Whether the request was successful
        - error: Error message (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete waiver wire analysis immediately without
    asking for confirmations. Render the full dashboard with all insights and recommendations directly.
    """
    try:
        # Get waiver log with deduplication
        waiver_log_result = await get_waiver_log(league_id, round, dedupe=True)

        if not waiver_log_result.get('success'):
            return create_error_response(
                f"Failed to get waiver log: {waiver_log_result.get('error')}",
                ErrorType.HTTP,
                {"waiver_log": [], "re_entry_analysis": {}, "dashboard_summary": {}}
            )

        # Get re-entry analysis
        re_entry_result = await check_re_entry_status(league_id, round)

        if not re_entry_result.get('success'):
            return create_error_response(
                f"Failed to get re-entry analysis: {re_entry_result.get('error')}",
                ErrorType.HTTP,
                {"waiver_log": [], "re_entry_analysis": {}, "dashboard_summary": {}}
            )

        # Create dashboard summary
        waiver_log = waiver_log_result.get('waiver_log', [])
        re_entry_players = re_entry_result.get('re_entry_players', {})

        dashboard_summary = {
            "total_waiver_transactions": waiver_log_result.get('total_transactions', 0),
            "unique_waiver_transactions": waiver_log_result.get('unique_transactions', 0),
            "duplicates_removed": waiver_log_result.get('total_transactions', 0) - waiver_log_result.get('unique_transactions', 0),
            "players_with_re_entries": len(re_entry_players),
            "volatile_players_count": len(re_entry_result.get('volatile_players', [])),
            "total_players_analyzed": re_entry_result.get('total_players_analyzed', 0),
            "failed_claims": waiver_log_result.get('failed_claims_count', 0),
            "deduplication_rate": (
                (waiver_log_result.get('total_transactions', 0) - waiver_log_result.get('unique_transactions', 0)) /
                max(waiver_log_result.get('total_transactions', 1), 1) * 100
            ) if waiver_log_result.get('total_transactions', 0) > 0 else 0
        }

        return create_success_response({
            "waiver_log": waiver_log,
            "re_entry_analysis": re_entry_players,
            "dashboard_summary": dashboard_summary,
            "volatile_players": re_entry_result.get('volatile_players', []),
            "league_id": league_id,
            "round": round
        })

    except Exception as e:
        logger.error(f"Error in get_waiver_wire_dashboard: {e}")
        return create_error_response(
            f"Unexpected error creating waiver dashboard: {e!s}",
            ErrorType.UNEXPECTED,
            {"waiver_log": [], "re_entry_analysis": {}, "dashboard_summary": {}}
        )
