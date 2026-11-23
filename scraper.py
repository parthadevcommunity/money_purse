import os
import re

def retrieve_context_for_llm(query_keyword, folder="llm_knowledge_base"):
    """Scans all historical records to construct a dense context package for the LLM."""
    context_blocks = []
    pattern = re.compile(rf"\b{re.escape(query_keyword)}\b", re.IGNORECASE)

    if not os.path.exists(folder):
        return "No data base available."

    for file in os.listdir(folder):
        if file.endswith(".md"):
            with open(os.path.join(folder, file), "r", encoding="utf-8") as f:
                content = f.read()
            
            if pattern.search(content):
                # Extract the video title and relevant sections
                title_line = content.split("\n")[0]
                context_blocks.append(f"--- Document Source: {title_line} ---\n{content[:2000]}\n")

    combined_context = "\n".join(context_blocks)
    
    # Construct the final prompt to feed your LLM
    llm_prompt = f"""
You are a financial analyst reviewing data from the 'Money Purse - Daily Money Show' playlist.
Below is the verified English-translated context extracted from historical channel episodes regarding '{query_keyword}':

{combined_context}

Based ONLY on the channel transcript data provided above, answer this question:
Question: What structural information, management viewpoints, or performance parameters did the channel present about {query_keyword}?
"""
    return llm_prompt

if __name__ == "__main__":
    # Example: Generate an instant prompt block for your local LLM
    target_stock = "APL Apollo"
    prompt_for_llm = retrieve_context_for_llm(target_stock)
    
    print("\n=== COPY AND PASTE THIS DIRECTLY INTO YOUR LLM ===")
    print(prompt_for_llm)
