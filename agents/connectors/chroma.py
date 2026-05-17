import json
import logging
import os
import time

from langchain_openai import OpenAIEmbeddings
from langchain_community.document_loaders import JSONLoader
from langchain_community.vectorstores.chroma import Chroma

from agents.polymarket.gamma import GammaMarketClient
from agents.utils.objects import SimpleEvent, SimpleMarket

logger = logging.getLogger(__name__)

def _make_embedding_function() -> OpenAIEmbeddings:
    """Return an OpenAIEmbeddings instance.

    Raises AuthenticationError / RateLimitError on first use if the key
    is invalid or quota is exhausted — callers handle this.
    """
    return OpenAIEmbeddings(model="text-embedding-3-small")


class PolymarketRAG:
    def __init__(self, local_db_directory=None, embedding_function=None) -> None:
        self.gamma_client = GammaMarketClient()
        self.local_db_directory = local_db_directory
        self.embedding_function = embedding_function

    def load_json_from_local(
        self, json_file_path=None, vector_db_directory="./local_db"
    ) -> None:
        loader = JSONLoader(
            file_path=json_file_path, jq_schema=".[].description", text_content=False
        )
        loaded_docs = loader.load()

        embedding_function = OpenAIEmbeddings(model="text-embedding-3-small")
        Chroma.from_documents(
            loaded_docs, embedding_function, persist_directory=vector_db_directory
        )

    def create_local_markets_rag(self, local_directory="./local_db") -> None:
        all_markets = self.gamma_client.get_all_current_markets()

        if not os.path.isdir(local_directory):
            os.mkdir(local_directory)

        local_file_path = f"{local_directory}/all-current-markets_{time.time()}.json"

        with open(local_file_path, "w+") as output_file:
            json.dump(all_markets, output_file)

        self.load_json_from_local(
            json_file_path=local_file_path, vector_db_directory=local_directory
        )

    def query_local_markets_rag(
        self, local_directory=None, query=None
    ) -> "list[tuple]":
        embedding_function = OpenAIEmbeddings(model="text-embedding-3-small")
        local_db = Chroma(
            persist_directory=local_directory, embedding_function=embedding_function
        )
        response_docs = local_db.similarity_search_with_score(query=query)
        return response_docs

    def events(self, events: "list[SimpleEvent]", prompt: str) -> "list[tuple]":
        # create local json file
        local_events_directory: str = "./local_db_events"
        if not os.path.isdir(local_events_directory):
            os.mkdir(local_events_directory)
        local_file_path = f"{local_events_directory}/events.json"
        dict_events = [x.dict() for x in events]
        with open(local_file_path, "w+") as output_file:
            json.dump(dict_events, output_file)

        # create vector db
        def metadata_func(record: dict, metadata: dict) -> dict:

            metadata["id"] = record.get("id")
            metadata["markets"] = record.get("markets")

            return metadata

        loader = JSONLoader(
            file_path=local_file_path,
            jq_schema=".[]",
            content_key="description",
            text_content=False,
            metadata_func=metadata_func,
        )
        loaded_docs = loader.load()
        try:
            embedding_function = _make_embedding_function()
            vector_db_directory = f"{local_events_directory}/chroma"
            local_db = Chroma.from_documents(
                loaded_docs, embedding_function, persist_directory=vector_db_directory
            )
            return local_db.similarity_search_with_score(query=prompt)
        except Exception as exc:
            _is_quota = (
                "insufficient_quota" in str(exc)
                or "exceeded your current quota" in str(exc)
                or "RateLimitError" in type(exc).__name__
                or "AuthenticationError" in type(exc).__name__
            )
            if _is_quota:
                _limit = 30
                logger.warning(
                    "OpenAI embeddings quota exhausted — bypassing RAG filter, "
                    "returning first %d of %d events (tag=events)",
                    _limit,
                    len(loaded_docs),
                )
                return [(doc, 0.0) for doc in loaded_docs[:_limit]]
            raise

    def markets(self, markets: "list[SimpleMarket]", prompt: str) -> "list[tuple]":
        # create local json file
        local_events_directory: str = "./local_db_markets"
        if not os.path.isdir(local_events_directory):
            os.mkdir(local_events_directory)
        local_file_path = f"{local_events_directory}/markets.json"
        with open(local_file_path, "w+") as output_file:
            json.dump(markets, output_file)

        # create vector db
        def metadata_func(record: dict, metadata: dict) -> dict:

            metadata["id"] = record.get("id")
            metadata["outcomes"] = record.get("outcomes")
            metadata["outcome_prices"] = record.get("outcome_prices")
            metadata["question"] = record.get("question")
            metadata["clob_token_ids"] = record.get("clob_token_ids")

            return metadata

        loader = JSONLoader(
            file_path=local_file_path,
            jq_schema=".[]",
            content_key="description",
            text_content=False,
            metadata_func=metadata_func,
        )
        loaded_docs = loader.load()
        try:
            embedding_function = _make_embedding_function()
            vector_db_directory = f"{local_events_directory}/chroma"
            local_db = Chroma.from_documents(
                loaded_docs, embedding_function, persist_directory=vector_db_directory
            )
            return local_db.similarity_search_with_score(query=prompt)
        except Exception as exc:
            _is_quota = (
                "insufficient_quota" in str(exc)
                or "exceeded your current quota" in str(exc)
                or "RateLimitError" in type(exc).__name__
                or "AuthenticationError" in type(exc).__name__
            )
            if _is_quota:
                _limit = 50
                logger.warning(
                    "OpenAI embeddings quota exhausted — bypassing RAG filter, "
                    "returning first %d of %d markets (tag=markets)",
                    _limit,
                    len(loaded_docs),
                )
                return [(doc, 0.0) for doc in loaded_docs[:_limit]]
            raise

