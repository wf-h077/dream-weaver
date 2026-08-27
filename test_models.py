from models import call_llm

try:
    response = call_llm(
        role="writer",
        prompt="hello context!",
        tools=[{"type": "function", "function": {"name": "test_tool", "description": "test", "parameters": {"type": "object", "properties": {}}}}],
    )
    print("Test passed. Response:", response)
except Exception as e:
    print("Test failed. Error:", e)
