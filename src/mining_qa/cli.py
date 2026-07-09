import argparse
import asyncio

from .agent import MiningQAAgent
from .config import get_settings
from .schemas import AskRequest


async def run(question: str) -> None:
    agent = MiningQAAgent(get_settings())
    response = await agent.ask(AskRequest(question=question))
    print(f"Status: {response.status}")
    print(response.answer)
    if response.knowledge_gap_task:
        print(f"\nKnowledge gap task: {response.knowledge_gap_task.task_id} ({response.knowledge_gap_task.status})")
    if response.sources:
        print("\nSources:")
        for source in response.sources:
            print(f"- {source.standard_no or ''} {source.title} {source.chapter or ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the Mining Knowledge QA agent.")
    parser.add_argument("question", help="Question to ask")
    args = parser.parse_args()
    asyncio.run(run(args.question))


if __name__ == "__main__":
    main()
