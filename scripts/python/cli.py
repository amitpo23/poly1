import typer
from devtools import pprint

from agents.polymarket.polymarket import Polymarket
from agents.connectors.chroma import PolymarketRAG
from agents.connectors.news import News
from agents.application.trade import Trader
from agents.application.executor import Executor
from agents.application.creator import Creator

app = typer.Typer()


@app.command()
def get_all_markets(limit: int = 5, sort_by: str = "spread") -> None:
    """
    Query Polymarket's markets
    """
    print(f"limit: int = {limit}, sort_by: str = {sort_by}")
    polymarket = Polymarket()
    markets = polymarket.get_all_markets()
    markets = polymarket.filter_markets_for_trading(markets)
    if sort_by == "spread":
        markets = sorted(markets, key=lambda x: x.spread, reverse=True)
    markets = markets[:limit]
    pprint(markets)


@app.command()
def get_relevant_news(keywords: str) -> None:
    """
    Use NewsAPI to query the internet
    """
    newsapi_client = News()
    articles = newsapi_client.get_articles_for_cli_keywords(keywords)
    pprint(articles)


@app.command()
def get_all_events(limit: int = 5, sort_by: str = "number_of_markets") -> None:
    """
    Query Polymarket's events
    """
    print(f"limit: int = {limit}, sort_by: str = {sort_by}")
    polymarket = Polymarket()
    events = polymarket.get_all_events()
    events = polymarket.filter_events_for_trading(events)
    if sort_by == "number_of_markets":
        events = sorted(events, key=lambda x: len(x.markets), reverse=True)
    events = events[:limit]
    pprint(events)


@app.command()
def create_local_markets_rag(local_directory: str) -> None:
    """
    Create a local markets database for RAG
    """
    polymarket_rag = PolymarketRAG()
    polymarket_rag.create_local_markets_rag(local_directory=local_directory)


@app.command()
def query_local_markets_rag(vector_db_directory: str, query: str) -> None:
    """
    RAG over a local database of Polymarket's events
    """
    polymarket_rag = PolymarketRAG()
    response = polymarket_rag.query_local_markets_rag(
        local_directory=vector_db_directory, query=query
    )
    pprint(response)


@app.command()
def ask_superforecaster(event_title: str, market_question: str, outcome: str) -> None:
    """
    Ask a superforecaster about a trade
    """
    print(
        f"event: str = {event_title}, question: str = {market_question}, outcome (usually yes or no): str = {outcome}"
    )
    executor = Executor()
    response = executor.get_superforecast(
        event_title=event_title, market_question=market_question, outcome=outcome
    )
    print(f"Response:{response}")


@app.command()
def create_market() -> None:
    """
    Format a request to create a market on Polymarket
    """
    c = Creator()
    market_description = c.one_best_market()
    print(f"market_description: str = {market_description}")


@app.command()
def ask_llm(user_input: str) -> None:
    """
    Ask a question to the LLM and get a response.
    """
    executor = Executor()
    response = executor.get_llm_response(user_input)
    print(f"LLM Response: {response}")


@app.command()
def ask_polymarket_llm(user_input: str) -> None:
    """
    What types of markets do you want trade?
    """
    executor = Executor()
    response = executor.get_polymarket_llm(user_input=user_input)
    print(f"LLM + current markets&events response: {response}")


@app.command()
def run_autonomous_trader(
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually submit the trade. Default is dry-run.",
    ),
    max_retries: int = typer.Option(3, help="Maximum attempts before failing."),
    max_position_fraction: float = typer.Option(
        0.10,
        help="Maximum fraction of USDC balance to use in one trade.",
    ),
    min_confidence: float = typer.Option(
        0.0,
        help="Skip trades below this confidence when confidence is provided.",
    ),
    top_n: int = typer.Option(3, help="Top markets to evaluate per cycle."),
    max_trades_per_cycle: int = typer.Option(
        2, help="Cap trades per cycle to limit blast radius."
    ),
) -> None:
    """
    Run a single trade cycle (one sweep over top-N markets).
    """
    trader = Trader(
        dry_run=not execute,
        max_retries=max_retries,
        max_position_fraction=max_position_fraction,
        min_confidence=min_confidence,
        top_n=top_n,
        max_trades_per_cycle=max_trades_per_cycle,
    )
    trader.one_best_trade()


@app.command()
def run_loop(
    execute: bool = typer.Option(
        False, "--execute", help="Actually submit trades. Default is dry-run."
    ),
    poll_seconds: int = typer.Option(1800, help="Seconds between cycles."),
    jitter_seconds: int = typer.Option(30, help="Random jitter added to poll."),
    top_n: int = typer.Option(3),
    max_trades_per_cycle: int = typer.Option(2),
    max_position_fraction: float = typer.Option(0.05),
    min_confidence: float = typer.Option(0.60),
) -> None:
    """
    Run the trader as a continuous daemon (the production entrypoint).
    """
    from agents.application.cron import TraderDaemon
    from agents.utils.logging_setup import configure_logging

    configure_logging()
    trader = Trader(
        dry_run=not execute,
        top_n=top_n,
        max_trades_per_cycle=max_trades_per_cycle,
        max_position_fraction=max_position_fraction,
        min_confidence=min_confidence,
    )
    daemon = TraderDaemon(
        trader=trader,
        poll_seconds=poll_seconds,
        jitter_seconds=jitter_seconds,
    )
    daemon.start()


@app.command()
def inspect_trades(limit: int = typer.Option(20, help="Rows to show")) -> None:
    """
    Pretty-print recent rows from the trade journal.
    """
    from agents.application.trade_log import TradeLog

    rows = TradeLog().recent(limit=limit)
    if not rows:
        print("(no trades logged yet)")
        return
    for r in rows:
        print(
            f"{r['ts']}  status={r['status']:<18s}  market={r['market_id']:<10s}  "
            f"side={r['side'] or '-':<5s}  price={r['price'] or '-':<6}  "
            f"size_usdc={r['size_usdc'] or '-'}  confidence={r['confidence'] or '-'}  "
            f"err={r['error'] or '-'}"
        )


if __name__ == "__main__":
    app()
