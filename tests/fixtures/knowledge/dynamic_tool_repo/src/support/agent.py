from crewai import Agent, Crew, Task

from .tools import _initialize_tools

MODEL = "gemini/gemini-2.5-flash"


class SupportCrew:
    def build(self):
        tools = _initialize_tools()
        agent = Agent(
            role="support",
            goal="Answer support questions",
            backstory="You help customers with orders.",
            tools=list(tools.values()),
            llm=MODEL,
        )
        task = Task(description="Handle the customer request", agent=agent)
        return Crew(agents=[agent], tasks=[task])
