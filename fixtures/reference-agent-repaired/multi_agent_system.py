"""Disclosed repaired copy of the reference CrewAI/Gemini agent.

The mock tools are deterministic and intentionally remain registered through a
runtime dictionary so discovery can distinguish static tool classes from
runtime registration. Provider execution is still mediated by the evaluation
engine's proxy when this fixture is run.
"""

from __future__ import annotations

import random
from typing import Any

from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field


class QueryInput(BaseModel):
    query: str = Field(min_length=1)


class _DeterministicTool(BaseTool):
    seed: int = 17

    def _rng(self, query: str) -> random.Random:
        return random.Random(f"{self.seed}:{query}")


class MockWebSearchTool(_DeterministicTool):
    name: str = "web_search"
    description: str = "Return deterministic mock web results for a query."

    def _run(self, query: str) -> str:
        score = self._rng(query).random()
        return f"web result for {query!r}; confidence={score:.3f}"


class MockSQLTool(_DeterministicTool):
    name: str = "sql_query"
    description: str = "Return deterministic mock structured data for a query."

    def _run(self, query: str) -> str:
        count = self._rng(query).randint(1, 5)
        return f"structured result for {query!r}; rows={count}"


class MockVectorSearchTool(_DeterministicTool):
    name: str = "vector_search"
    description: str = "Return deterministic mock semantic matches for a query."

    def _run(self, query: str) -> str:
        score = self._rng(query).uniform(0.5, 0.99)
        return f"semantic result for {query!r}; similarity={score:.3f}"


class MultiAgentSystem:
    """Manager plus three specialist agents using an explicit model binding."""

    def __init__(self, gemini_api_key: str):
        if not isinstance(gemini_api_key, str) or not gemini_api_key:
            raise ValueError("gemini_api_key must be supplied by the runtime")
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            google_api_key=gemini_api_key,
        )
        self.tools = self._initialize_tools()
        self.agents = self._create_agents()

    def _initialize_tools(self) -> dict[str, BaseTool]:
        """Keep runtime registration explicit for discovery and review."""
        return {
            "web_search": MockWebSearchTool(),
            "sql_query": MockSQLTool(),
            "vector_search": MockVectorSearchTool(),
        }

    def _create_agents(self) -> dict[str, Agent]:
        manager = Agent(
            role="Query manager",
            goal="Coordinate specialists and synthesize an answer",
            backstory="Delegate work and identify uncertainty honestly.",
            llm=self.llm,
            allow_delegation=True,
            verbose=False,
        )
        web = Agent(
            role="Web research specialist",
            goal="Find relevant web evidence",
            backstory="Return concise, source-aware findings.",
            tools=[self.tools["web_search"]],
            llm=self.llm,
            verbose=False,
        )
        sql = Agent(
            role="Structured data specialist",
            goal="Analyze structured data",
            backstory="Return precise tabular findings.",
            tools=[self.tools["sql_query"]],
            llm=self.llm,
            verbose=False,
        )
        vector = Agent(
            role="Semantic search specialist",
            goal="Find related concepts",
            backstory="Return similarity findings with limitations.",
            tools=[self.tools["vector_search"]],
            llm=self.llm,
            verbose=False,
        )
        return {"manager": manager, "web": web, "sql": sql, "vector": vector}

    def _create_tasks(self, query: QueryInput) -> list[Task]:
        return [
            Task(
                description=f"Research current web evidence for: {query.query}",
                expected_output="Concise web findings with source limitations.",
                agent=self.agents["web"],
            ),
            Task(
                description=f"Analyze structured data relevant to: {query.query}",
                expected_output="Concise structured-data findings.",
                agent=self.agents["sql"],
            ),
            Task(
                description=f"Find semantic matches for: {query.query}",
                expected_output="Concise semantic findings with similarity limitations.",
                agent=self.agents["vector"],
            ),
            Task(
                description=(
                    f"Synthesize the specialist findings into an answer for: {query.query}. "
                    "Do not invent sources or execution evidence."
                ),
                expected_output="A synthesized answer with uncertainty and source attribution.",
                agent=self.agents["manager"],
            ),
        ]

    def kickoff(self, query: str) -> Any:
        """Run one explicit evaluation invocation through CrewAI."""
        parsed = QueryInput(query=query)
        crew = Crew(
            agents=list(self.agents.values()),
            tasks=self._create_tasks(parsed),
            process=Process.sequential,
            verbose=False,
        )
        return crew.kickoff()


def kickoff(query: str, gemini_api_key: str) -> Any:
    """Convenience entrypoint used by the reviewed runtime declaration."""
    return MultiAgentSystem(gemini_api_key).kickoff(query)


def main() -> None:
    """CLI entrypoint; the runtime supplies the key explicitly."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--gemini-api-key", required=True)
    args = parser.parse_args()
    print(kickoff(args.query, args.gemini_api_key))


if __name__ == "__main__":
    main()
