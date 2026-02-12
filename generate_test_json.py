import asyncio
import json
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "apps/execution-plane/src"))

from activities.discoveryActivities import generate_react_flow_node

async def main():
    steps = [
        {
            "node_id": "1",
            "action": "navigate",
            "intent": "Go to GitHub Trending",
            "selector": "https://github.com/trending",
            "value": "https://github.com/trending",
            "position": {"x": 100, "y": 100}
        },
        {
            "node_id": "2",
            "action": "click",
            "intent": "First Repository",
            "selector": "article h1 a",  # Realistic selector for repo title
            "position": {"x": 100, "y": 250},
            # This simulates the "Visual Sort" logic where we pick the first one
            # The backend execution uses the intent/selector, but here we just mock the node
        },
        {
            "node_id": "3",
            "action": "wait",
            "intent": "Wait for load",
            "value": "2000",
            "selector": "",
            "position": {"x": 100, "y": 400}
        }
    ]

    nodes = []
    edges = []

    for i, step in enumerate(steps):
        node = await generate_react_flow_node(step)
        nodes.append(node)

        if i > 0:
            edges.append({
                "id": f"e{i}-{i+1}",
                "source": steps[i-1]["node_id"],
                "target": step["node_id"],
                "type": "smoothstep"
            })

    workflow = {
        "nodes": nodes,
        "edges": edges,
        "viewport": {"x": 0, "y": 0, "zoom": 1}
    }

    print(json.dumps(workflow, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
