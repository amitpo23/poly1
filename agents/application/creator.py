from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket

import logging
import time


logger = logging.getLogger(__name__)


class Creator:
    def __init__(self, max_retries: int = 3, retry_delay_seconds: int = 5):
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.polymarket = Polymarket()
        self.gamma = Gamma()
        self.agent = Agent()

    def one_best_market(self):
        """

        one_best_trade is a strategy that evaluates all events, markets, and orderbooks

        leverages all available information sources accessible to the autonomous agent

        then executes that trade without any human intervention

        """
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._one_best_market_once()
            except Exception:
                logger.exception(
                    "Market creation attempt %s/%s failed",
                    attempt,
                    self.max_retries,
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(self.retry_delay_seconds)

    def _one_best_market_once(self):
        events = self.polymarket.get_all_tradeable_events()
        print(f"1. FOUND {len(events)} EVENTS")

        filtered_events = self.agent.filter_events_with_rag(events)
        print(f"2. FILTERED {len(filtered_events)} EVENTS")

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        print()
        print(f"3. FOUND {len(markets)} MARKETS")

        print()
        filtered_markets = self.agent.filter_markets(markets)
        print(f"4. FILTERED {len(filtered_markets)} MARKETS")

        best_market = self.agent.source_best_market_to_create(filtered_markets)
        print(f"5. IDEA FOR NEW MARKET {best_market}")
        return best_market

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = Creator()
    c.one_best_market()
