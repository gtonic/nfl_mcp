"""Tool-routing scenarios for the agent eval.

Each scenario is a realistic user prompt plus the tool(s) a good assistant should
call. ``expect`` is an *any-of* list (several tools can be reasonable). ``args``
optionally asserts the model extracted an obvious argument — value ``None`` means
"just present".
"""

SCENARIOS = [
    # --- Drafting -----------------------------------------------------------
    {
        "id": "draft_pick_live",
        "prompt": "I'm in Sleeper draft 998877 at draft slot 7 and I'm on the clock. Who should I take right now?",
        "expect": ["recommend_draft_pick"],
        "args": {"draft_id": "998877", "my_slot": 7},
    },
    {
        "id": "draft_board",
        "prompt": "Build me a draft board for a 12-team full-PPR redraft league.",
        "expect": ["get_draft_board"],
    },
    {
        "id": "simulate_draft",
        "prompt": "Run 100 mock drafts from the 3rd pick in a 12-team PPR league and show my likely roster.",
        "expect": ["simulate_draft"],
        "args": {"my_slot": 3},
    },
    # --- Values & trades ----------------------------------------------------
    {
        "id": "player_value",
        "prompt": "What is Bijan Robinson worth in a 12-team PPR league?",
        "expect": ["get_player_value", "get_player_values"],
    },
    {
        "id": "trade_fairness",
        "prompt": "In league 555, is it fair if roster 1 gives players 4034 and 4035 to roster 2 for player 4046?",
        "expect": ["analyze_trade"],
        "args": {"league_id": "555"},
    },
    # --- Weekly management --------------------------------------------------
    {
        "id": "faab_bid",
        "prompt": "How much of my FAAB budget should I bid on player 6790 in league 555 for my roster 3?",
        "expect": ["recommend_faab_bid"],
        "args": {"league_id": "555"},
    },
    {
        "id": "lineup",
        "prompt": "Here's my week-6 roster with players, teams and opponents — set my optimal starting lineup.",
        "expect": ["analyze_full_lineup", "get_roster_recommendations"],
    },
    {
        "id": "start_sit_compare",
        "prompt": "Should I start Player A (WR, MIA vs NE) or Player B (RB, SF vs ARI) in my flex this week?",
        "expect": ["compare_players_for_slot", "get_start_sit_recommendation", "get_roster_recommendations"],
    },
    {
        "id": "matchup",
        "prompt": "How tough is the WR matchup against the Kansas City defense?",
        "expect": ["get_matchup_difficulty"],
    },
    {
        "id": "injuries",
        "prompt": "Give me the high-confidence injuries for KC and SF.",
        "expect": ["get_high_confidence_injuries", "get_injury_report"],
    },
    {
        "id": "waiver_dashboard",
        "prompt": "Show me the waiver-wire dashboard for league 555.",
        "expect": ["get_waiver_wire_dashboard", "get_waiver_log"],
        "args": {"league_id": "555"},
    },
    {
        "id": "trending",
        "prompt": "Who are the most-added players across Sleeper right now?",
        "expect": ["get_trending_players"],
    },
    # --- Strategy -----------------------------------------------------------
    {
        "id": "playoff_odds",
        "prompt": "What are my playoff odds in league 555?",
        "expect": ["get_playoff_odds"],
        "args": {"league_id": "555"},
    },
    {
        "id": "find_leagues",
        "prompt": "My Sleeper username is gridiron_gary — find my leagues for the 2026 season.",
        "expect": ["get_user", "get_user_leagues"],
    },
    {
        "id": "stacks",
        "prompt": "Which games this week are best for a QB + WR stack?",
        "expect": ["get_stack_opportunities", "get_vegas_lines"],
    },
]
