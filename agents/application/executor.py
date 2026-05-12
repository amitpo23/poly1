import logging
import os
import json
import ast
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import math

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
try:
    import anthropic as _anthropic_sdk
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


logger = logging.getLogger(__name__)

from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.connectors.chroma import PolymarketRAG as Chroma
from agents.utils.objects import SimpleEvent, SimpleMarket, TradeRecommendation
from agents.application.prompts import Prompter
from agents.application.trade_recommendation import (
    parse_trade_recommendation as parse_trade_recommendation_text,
)
from agents.polymarket.polymarket import Polymarket

def retain_keys(data, keys_to_retain):
    if isinstance(data, dict):
        return {
            key: retain_keys(value, keys_to_retain)
            for key, value in data.items()
            if key in keys_to_retain
        }
    elif isinstance(data, list):
        return [retain_keys(item, keys_to_retain) for item in data]
    else:
        return data

DEFAULT_TOKEN_PRICING_PER_1K = {
    "gpt-3.5-turbo-16k": {"prompt": 0.003, "completion": 0.004},
    "gpt-4-1106-preview": {"prompt": 0.01, "completion": 0.03},
    "gpt-4o": {"prompt": 0.005, "completion": 0.015},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
}


class Executor:
    def __init__(self, default_model: Optional[str] = None) -> None:
        load_dotenv()
        model = default_model or os.getenv("OPENAI_MODEL", "gpt-3.5-turbo-16k")
        max_token_model = {'gpt-3.5-turbo-16k': 15000, 'gpt-4-1106-preview': 95000}
        self.token_limit = max_token_model.get(model, 15000)
        self.model = model
        self.prompter = Prompter()
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        # For models that support JSON-mode (gpt-4o, gpt-4o-mini), pass
        # response_format so the LLM is structurally constrained to emit
        # valid JSON.  Older models (gpt-3.5-turbo-16k) don't support this
        # parameter and get the standard ChatOpenAI constructor.
        _JSON_MODE_MODELS = {"gpt-4o", "gpt-4o-mini"}
        if model in _JSON_MODE_MODELS:
            self.llm = ChatOpenAI(
                model=model,
                temperature=0,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        else:
            self.llm = ChatOpenAI(
                model=model,
                temperature=0,
            )
        self._gamma = None
        self._chroma = None
        self._polymarket = None
        self.llm_usage_path = Path(
            os.getenv("LLM_USAGE_FILE", "./data/llm_usage.jsonl")
        )

    @property
    def gamma(self):
        if self._gamma is None:
            self._gamma = Gamma()
        return self._gamma

    @property
    def chroma(self):
        if self._chroma is None:
            self._chroma = Chroma()
        return self._chroma

    @property
    def polymarket(self):
        if self._polymarket is None:
            # Executor only needs read-only helpers (map_api_to_market). Skip CLOB init.
            self._polymarket = Polymarket(live=False)
        return self._polymarket

    def _invoke_tracked(self, messages, tag: str) -> str:
        try:
            result = self.llm.invoke(messages)
        except Exception as exc:
            _is_quota = (
                "insufficient_quota" in str(exc)
                or "exceeded your current quota" in str(exc)
                or "RateLimitError" in type(exc).__name__
            )
            anthropic_key = os.getenv("ANTHROPIC_API_KEY")
            if _is_quota and anthropic_key and _ANTHROPIC_AVAILABLE:
                logger.warning(
                    "OpenAI quota exhausted — falling back to %s (tag=%s)",
                    _ANTHROPIC_MODEL,
                    tag,
                )
                _client = _anthropic_sdk.Anthropic(api_key=anthropic_key)
                # Convert LangChain messages (or plain string) to Anthropic format
                _system = None
                _anth_msgs = []
                if isinstance(messages, str):
                    # Plain string prompt — treat as a single user message
                    _anth_msgs = [{"role": "user", "content": messages.strip()}]
                else:
                    for _m in messages:
                        if isinstance(_m, SystemMessage):
                            _system = _m.content
                        elif isinstance(_m, HumanMessage):
                            _anth_msgs.append({"role": "user", "content": _m.content.strip()})
                        else:
                            _content = getattr(_m, 'content', str(_m)).strip()
                            if _content:  # skip empty assistant turns
                                _anth_msgs.append({"role": "assistant", "content": _content})
                _kwargs = {
                    "model": _ANTHROPIC_MODEL,
                    "max_tokens": 4096,
                    "messages": _anth_msgs,
                }
                if _system:
                    _kwargs["system"] = _system
                _resp = _client.messages.create(**_kwargs)
                return _resp.content[0].text
            raise
        try:
            self._record_usage(result, tag)
        except Exception:
            logger.exception("llm usage record failed (tag=%s)", tag)
        return result.content

    def _record_usage(self, result, tag: str) -> None:
        meta = getattr(result, "response_metadata", None) or {}
        usage = meta.get("token_usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        pricing = DEFAULT_TOKEN_PRICING_PER_1K.get(self.model, {})
        est_cost = (
            prompt_tokens / 1000.0 * pricing.get("prompt", 0.0)
            + completion_tokens / 1000.0 * pricing.get("completion", 0.0)
        )
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "model": self.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "est_cost_usd": round(est_cost, 6),
        }
        self.llm_usage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.llm_usage_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def get_llm_response(self, user_input: str) -> str:
        system_message = SystemMessage(content=str(self.prompter.market_analyst()))
        human_message = HumanMessage(content=user_input)
        messages = [system_message, human_message]
        return self._invoke_tracked(messages, tag="get_llm_response")

    def get_superforecast(
        self, event_title: str, market_question: str, outcome: str
    ) -> str:
        messages = self.prompter.superforecaster(
            description=event_title, question=market_question, outcome=outcome
        )
        return self._invoke_tracked(messages, tag="get_superforecast")

    def estimate_tokens(self, text: str) -> int:
        # Rough estimate; sufficient for chunking decisions only (not cost).
        return len(text) // 4

    def process_data_chunk(self, data1: List[Dict[Any, Any]], data2: List[Dict[Any, Any]], user_input: str) -> str:
        system_message = SystemMessage(
            content=str(self.prompter.prompts_polymarket(data1=data1, data2=data2))
        )
        human_message = HumanMessage(content=user_input)
        messages = [system_message, human_message]
        return self._invoke_tracked(messages, tag="process_data_chunk")


    def divide_list(self, original_list, i):
        # Calculate the size of each sublist
        sublist_size = math.ceil(len(original_list) / i)
        
        # Use list comprehension to create sublists
        return [original_list[j:j+sublist_size] for j in range(0, len(original_list), sublist_size)]
    
    def get_polymarket_llm(self, user_input: str) -> str:
        data1 = self.gamma.get_current_events()
        data2 = self.gamma.get_current_markets()
        
        combined_data = str(self.prompter.prompts_polymarket(data1=data1, data2=data2))
        
        # Estimate total tokens
        total_tokens = self.estimate_tokens(combined_data)
        
        # Set a token limit (adjust as needed, leaving room for system and user messages)
        token_limit = self.token_limit
        if total_tokens <= token_limit:
            # If within limit, process normally
            return self.process_data_chunk(data1, data2, user_input)
        else:
            # If exceeding limit, process in chunks
            chunk_size = len(combined_data) // ((total_tokens // token_limit) + 1)
            logger.info("total tokens %s exceeding llm capacity, splitting", total_tokens)
            group_size = (total_tokens // token_limit) + 1 # 3 is safe factor
            keys_no_meaning = ['image','pagerDutyNotificationEnabled','resolvedBy','endDate','clobTokenIds','negRiskMarketID','conditionId','updatedAt','startDate']
            useful_keys = ['id','questionID','description','liquidity','clobTokenIds','outcomes','outcomePrices','volume','startDate','endDate','question','questionID','events']
            data1 = retain_keys(data1, useful_keys)
            cut_1 = self.divide_list(data1, group_size)
            cut_2 = self.divide_list(data2, group_size)
            cut_data_12 = zip(cut_1, cut_2)

            results = []

            for cut_data in cut_data_12:
                sub_data1 = cut_data[0]
                sub_data2 = cut_data[1]
                sub_tokens = self.estimate_tokens(str(self.prompter.prompts_polymarket(data1=sub_data1, data2=sub_data2)))

                result = self.process_data_chunk(sub_data1, sub_data2, user_input)
                results.append(result)
            
            combined_result = " ".join(results)
            
        
            
            return combined_result
    def filter_events_with_rag(self, events: "list[SimpleEvent]") -> str:
        prompt = self.prompter.filter_events()
        logger.debug("filter_events_with_rag prompt: %s", prompt)
        return self.chroma.events(events, prompt)

    def map_filtered_events_to_markets(
        self, filtered_events: "list[SimpleEvent]"
    ) -> "list[SimpleMarket]":
        markets = []
        for e in filtered_events:
            data = json.loads(e[0].json())
            market_ids = data["metadata"]["markets"].split(",")
            for market_id in market_ids:
                try:
                    market_data = self.gamma.get_market(market_id)
                    formatted_market_data = self.polymarket.map_api_to_market(market_data)
                except Exception as exc:
                    logger.warning("Skipping market %s: fetch/map failed: %s", market_id, exc)
                    continue
                # Skip markets with degenerate prices/outcomes — post-migration
                # Gamma sometimes returns missing or zero prices, which causes
                # the LLM to default to price=0.0 and trip pre-order validation.
                try:
                    prices = ast.literal_eval(formatted_market_data.get("outcome_prices") or "[]")
                    outcomes = ast.literal_eval(formatted_market_data.get("outcomes") or "[]")
                except (ValueError, SyntaxError):
                    logger.warning("Skipping market %s: unparseable outcomes/prices", market_id)
                    continue
                if (
                    len(prices) < 2 or len(outcomes) < 2
                    or any(float(p) <= 0 or float(p) >= 1 for p in prices)
                ):
                    logger.info(
                        "Skipping degenerate market %s: prices=%s outcomes=%s",
                        market_id, prices, outcomes,
                    )
                    continue
                markets.append(formatted_market_data)
        return markets

    def filter_markets(self, markets) -> "list[tuple]":
        prompt = self.prompter.filter_markets()
        logger.debug("filter_markets prompt: %s", prompt)
        return self.chroma.markets(markets, prompt)

    def source_best_trade(self, market_object) -> str:
        market_document = market_object[0].dict()
        market = market_document["metadata"]
        outcome_prices = ast.literal_eval(market["outcome_prices"])
        outcomes = ast.literal_eval(market["outcomes"])
        question = market["question"]
        description = market_document["page_content"]

        prompt = self.prompter.superforecaster(question, description, outcomes)
        logger.debug("superforecaster prompt: %s", prompt)
        forecast = self._invoke_tracked(prompt, tag="superforecaster")
        logger.debug("superforecaster result: %s", forecast)

        prompt = self.prompter.one_best_trade(forecast, outcomes, outcome_prices)
        logger.debug("one_best_trade prompt: %s", prompt)
        content = self._invoke_tracked(prompt, tag="one_best_trade")
        logger.debug("one_best_trade result: %s", content)
        return content

    def parse_trade_recommendation(self, best_trade: str) -> TradeRecommendation:
        return parse_trade_recommendation_text(best_trade)

    def source_best_market_to_create(self, filtered_markets) -> str:
        prompt = self.prompter.create_new_market(filtered_markets)
        logger.debug("create_new_market prompt: %s", prompt)
        return self._invoke_tracked(prompt, tag="source_best_market_to_create")
