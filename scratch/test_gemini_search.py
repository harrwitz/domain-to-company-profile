import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import asyncio
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, LLM_MODEL

async def test():
    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = await client.aio.models.generate_content(
        model=LLM_MODEL,
        contents='Find the LinkedIn profile URL and current employer of Lee Robinson. What is his current company and role? Give exact json with keys: name, current_company, role, linkedin_url.',
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    print('Gemini search response:')
    print(resp.text)
    if resp.candidates and resp.candidates[0].grounding_metadata:
        gm = resp.candidates[0].grounding_metadata
        print('Grounding search queries:', gm.web_search_queries)
        if gm.grounding_chunks:
            for c in gm.grounding_chunks[:3]:
                print('Grounding chunk:', c.web.uri if c.web else None, c.web.title if c.web else None)

if __name__ == "__main__":
    asyncio.run(test())
