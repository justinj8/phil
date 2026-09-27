"""Did Phil actually improve? The dashboard's one judgment call.

data.comparison() calls judge_improvement() once the run has at least
2 x window settled bets (window = config/run.json "comparison_window", 20 by
default), so the two windows never overlap. Each window is the first / last
`window` settled bets in the order they were PLACED, scored exactly the way
core/score.py scores everything:

    n                 bets in the window
    wins, losses      counts
    win_rate          wins / n
    pnl_usd           realized P&L of those bets
    roi               pnl / staked
    brier_agent       mean (est_prob - outcome)^2
    brier_market      mean (price paid - outcome)^2
    brier_delta       brier_agent - brier_market; NEGATIVE = Phil beat the price
    avg_edge          mean claimed edge (est_prob - fill) at placement
    per_trade_delta   each bet's (est - y)^2 - (price - y)^2; their mean is
                      brier_delta, their spread is the noise around it
    strategy_changes  lesson commits made while the window's bets were placed

The guide's rule of thumb: a better brier_delta in the last window means the
forecasting improved; better P&L with the same or worse brier_delta is
probably luck. The hard part is "better": with 20 bets per side, how big a
move is signal rather than noise?

Return a dict:
    {"verdict": "improved" | "luck" | "no-change" | "worse",
     "reason": "<one sentence the dashboard shows under the verdict>"}
"""


def judge_improvement(first: dict, last: dict) -> dict:
    """Compare the first and last windows of settled bets; return a verdict dict.

    Both arguments are window dicts as described in the module docstring.
    Until this is implemented the dashboard shows the two windows side by
    side with an "unjudged" verdict.
    """
    # TODO(operator): decide what counts as a real improvement. Options:
    #   - a fixed bar: last["brier_delta"] must beat first["brier_delta"] by
    #     at least some epsilon (e.g. 0.01) to count as "improved";
    #   - noise-aware: compare the change in mean per_trade_delta with its
    #     standard error, sqrt(var_first/n_first + var_last/n_last), and only
    #     call "improved" / "worse" when the change clears ~2 standard errors;
    #   - then separate "luck": P&L went up while brier_delta did not improve.
    raise NotImplementedError
