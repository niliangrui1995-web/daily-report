import logging
from daily_report import gemini_search

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def test_search():
    print("Testing Gemini Search (L1)...")
    res = gemini_search("AI应用", "OpenAI Sora 最新进展 2026", max_retries=1)
    
    print("\n--- Search Results ---")
    if res:
        print(f"Text length: {len(res['text'])}")
        print(f"Sources count: {len(res['grounding_sources'])}")
        for i, src in enumerate(res['grounding_sources'][:3], 1):
            print(f"  {i}. {src.get('title')} ({src.get('url')})")
        
        print("\nSnippet:")
        print(res['text'][:500])
    else:
        print("Search returned None.")

if __name__ == "__main__":
    test_search()
