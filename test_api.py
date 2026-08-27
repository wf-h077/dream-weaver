import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv("E:/网文/.env")

client = OpenAI(
    base_url=os.getenv("BASE_URL", "http://localhost:8000/v1"),
    api_key=os.getenv("API_KEY")
)

try:
    print("Test auto...")
    response = client.chat.completions.create(
        model="qwen3.5-9b",
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        tools=[{"type": "function", "function": {"name": "get_capital", "description": "Get capital", "parameters": {"type": "object", "properties": {}}}}],
        tool_choice="auto"
    )
    print("Auto success:", response)
except Exception as e:
    print("Auto failed:", e)

try:
    print("Test none...")
    response = client.chat.completions.create(
        model="qwen3.5-9b",
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        tools=[{"type": "function", "function": {"name": "get_capital", "description": "Get capital", "parameters": {"type": "object", "properties": {}}}}],
        tool_choice="none"
    )
    print("None success:", response)
except Exception as e:
    print("None failed:", e)

try:
    print("Test required...")
    response = client.chat.completions.create(
        model="qwen3.5-9b",
        messages=[{"role": "user", "content": "What is the capital of France?"}],
        tools=[{"type": "function", "function": {"name": "get_capital", "description": "Get capital", "parameters": {"type": "object", "properties": {}}}}],
        tool_choice="required"
    )
    print("Required success:", response)
except Exception as e:
    print("Required failed:", e)
